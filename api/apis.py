import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from advanced import ErrorCode, ExtractionError, classify_exception, get_config
from api.version_prefix import API_PREFIX
from engine import extract, precheck_request
from helpers import metrics, TaskStatus, get_health_status, get_status_manager, get_update_data
from helpers import auth as _auth, ratelimit as _rl

log = logging.getLogger("downflow.api")

router = APIRouter(prefix=API_PREFIX)


@router.get("/check-updates/")
async def check_updates():
    return {
        "status": "success",
        "data": get_update_data(),
    }


@router.get("/health/")
async def health_check():
    return {
        "status": "success",
        "data": get_health_status(),
    }


# Extraction (asynchronous tasks with live status)
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., max_length=8192, description="http(s) URL of the video page or media")
    metadata_only: bool = Field(False, description="Return metadata and formats without media URLs")
    format: Optional[str] = Field(
        None,
        max_length=200,
        description="best | best_single | worst | audio | video, or a yt-dlp format selector",
    )


class InstantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., max_length=8192, description="http(s) link of a single video")
    kind: Literal["video", "audio"] = "video"
    attempt: int = Field(0, ge=0, le=6, description="set by the client after a failed download to get a fresh route")


class _ExtractionRunner:
    # Runs extraction tasks in the background with bounded concurrency and queue size.

    def __init__(self) -> None:
        config = get_config()
        self.config = config
        self.manager = get_status_manager()
        self.semaphore = asyncio.Semaphore(config.max_concurrent)
        self.executor = ThreadPoolExecutor(max_workers=config.max_concurrent, thread_name_prefix="extract")
        self.waiting = 0
        self.shutting_down = False
        self._jobs: set[asyncio.Task] = set()

    def submit(self, request: ExtractRequest) -> dict:
        # Register a task and start it in the background; returns the initial status.
        if self.shutting_down:
            raise ExtractionError(ErrorCode.CANCELLED)
        if self.waiting >= self.config.max_queued:
            raise ExtractionError(ErrorCode.BUSY, retry_after=5)
        status = self.manager.create()
        self.waiting += 1
        job = asyncio.get_running_loop().create_task(self._run(status["task_id"], request))
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
        return status

    async def _run(self, task_id: str, request: ExtractRequest) -> None:
        config, manager = self.config, self.manager
        acquired = False
        try:
            try:
                await asyncio.wait_for(self.semaphore.acquire(), timeout=config.queue_wait)
                acquired = True
            except asyncio.TimeoutError:
                raise ExtractionError(ErrorCode.BUSY, retry_after=5) from None
            finally:
                self.waiting -= 1

            def progress(stage: str, message: str) -> None:
                manager.update(task_id, status=TaskStatus.PROCESSING, stage=stage, message=message)

            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(
                self.executor,
                lambda: extract(
                    request.url,
                    metadata_only=request.metadata_only,
                    format=request.format,
                    config=config,
                    progress=progress,
                    cancelled=lambda: self.shutting_down,
                ),
            )

            # The slot is released only when the worker thread really finishes, so
            # timed-out extractions cannot pile up beyond ``max_concurrent``.
            def _done(fut: "asyncio.Future") -> None:
                self.semaphore.release()
                if not fut.cancelled():
                    fut.exception()  # mark retrieved; the engine already logged any error

            acquired = False  # ownership of the slot moves to the future
            future.add_done_callback(_done)
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=config.request_timeout + 5)
            except asyncio.TimeoutError:
                log.warning("extraction exceeded %.0fs request limit", config.request_timeout)
                raise ExtractionError(ErrorCode.TIMEOUT) from None
            manager.complete(task_id, result)
        except ExtractionError as error:
            manager.fail(task_id, error.to_dict())
        except asyncio.CancelledError:
            manager.fail(task_id, ExtractionError(ErrorCode.CANCELLED).to_dict())
            raise
        except Exception as exc:  # noqa: BLE001 - never leak internals into task status
            log.exception("unexpected error in extraction task")
            manager.fail(task_id, classify_exception(exc).to_dict())
        finally:
            if acquired:
                self.semaphore.release()

    async def instant(self, request: InstantRequest) -> tuple[str, dict]:
        # Instant Save: extract the single best format (with retries) and register it as a downloadable task.
        from engine.instant import instant_extract

        config, manager = self.config, self.manager
        if self.shutting_down:
            raise ExtractionError(ErrorCode.CANCELLED)
        if self.waiting >= config.max_queued:
            raise ExtractionError(ErrorCode.BUSY, retry_after=5)
        status = manager.create()
        task_id = status["task_id"]
        self.waiting += 1
        try:
            await asyncio.wait_for(self.semaphore.acquire(), timeout=config.queue_wait)
        except asyncio.TimeoutError:
            manager.fail(task_id, ExtractionError(ErrorCode.BUSY, retry_after=5).to_dict())
            raise ExtractionError(ErrorCode.BUSY, retry_after=5) from None
        finally:
            self.waiting -= 1
        manager.update(task_id, status=TaskStatus.PROCESSING, stage="extracting_metadata",
                       message="Finding the best format")
        future = asyncio.get_running_loop().run_in_executor(
            self.executor,
            lambda: instant_extract(request.url, request.kind, config, attempt0=request.attempt,
                                    cancelled=lambda: self.shutting_down))

        def _done(fut: "asyncio.Future") -> None:
            self.semaphore.release()
            if not fut.cancelled():
                fut.exception()

        future.add_done_callback(_done)
        try:
            result = await asyncio.wait_for(asyncio.shield(future), timeout=config.instant_budget + 10)
        except asyncio.TimeoutError:
            err = ExtractionError(ErrorCode.TIMEOUT)
            manager.fail(task_id, err.to_dict())
            raise err from None
        except ExtractionError as err:
            manager.fail(task_id, err.to_dict())
            raise
        except Exception as exc:  # noqa: BLE001
            err = classify_exception(exc)
            manager.fail(task_id, err.to_dict())
            raise err from None
        manager.complete(task_id, result)
        return task_id, result

    async def shutdown(self) -> None:
        self.shutting_down = True
        for job in list(self._jobs):
            job.cancel()
        if self._jobs:
            await asyncio.gather(*self._jobs, return_exceptions=True)
        self.manager.fail_active(ExtractionError(ErrorCode.CANCELLED).to_dict())
        self.executor.shutdown(wait=False, cancel_futures=True)


_runner: Optional[_ExtractionRunner] = None


def _get_runner() -> _ExtractionRunner:
    global _runner
    if _runner is None:
        _runner = _ExtractionRunner()
    return _runner


async def shutdown_extraction() -> None:
    # Called from the application lifespan: stop tasks and mark them failed.
    if _runner is not None:
        await _runner.shutdown()


def _error_response(error: ExtractionError) -> JSONResponse:
    headers = {}
    if error.retry_after and error.http_status in (429, 503):
        headers["Retry-After"] = str(error.retry_after)
    return JSONResponse(
        status_code=error.http_status,
        content={"status": "error", "error": error.to_dict()},
        headers=headers,
    )


def _limited(request: Request, bucket: str) -> Optional[JSONResponse]:
    # Per-IP rate limit for the public endpoints (signed-in developers are exempt).
    if _auth.is_authed(request):
        return None
    wait = _rl.check(bucket, _auth.client_ip(request))
    if not wait:
        return None
    log.warning("rate limit %s · %s", bucket, _auth.client_ip(request))
    return _error_response(ExtractionError(ErrorCode.RATE_LIMITED, "Too many requests. Please wait a moment and try again.", retry_after=wait))


def _task_links(task_id: str) -> dict:
    return {
        "status_url": f"{API_PREFIX}/extract/status/{task_id}/",
        "result_url": f"{API_PREFIX}/extract/result/{task_id}/",
    }


@router.post("/extract/", status_code=202)
async def extract_video(payload: ExtractRequest, request: Request):
    # Validate the request, create a task and start extracting in the background.
    limited = _limited(request, "extract")
    if limited:
        return limited
    runner = _get_runner()
    try:
        # Reject bad input immediately (syntax, SSRF/DNS, format) before creating a task.
        await asyncio.to_thread(precheck_request, payload.url, payload.format, runner.config)
        status = runner.submit(payload)
    except ExtractionError as error:
        return _error_response(error)
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected error in /extract/")
        return _error_response(classify_exception(exc))
    return {
        "task_id": status["task_id"],
        "status": status["status"],
        "task": status,
        "links": _task_links(status["task_id"]),
        "error": None,
    }


@router.post("/instant/")
async def instant_save(payload: InstantRequest, request: Request):
    # Instant Save: one call, best format chosen automatically, up to 3 attempts. Then GET ``download_url``.
    from engine.instant import summarise

    limited = _limited(request, "extract")
    if limited:
        return limited
    runner = _get_runner()
    try:
        await asyncio.to_thread(precheck_request, payload.url, None, runner.config)
        task_id, result = await runner.instant(payload)
    except ExtractionError as error:
        metrics.inc("instant_fail")
        return _error_response(error)
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected error in /instant/")
        metrics.inc("instant_fail")
        return _error_response(classify_exception(exc))
    metrics.inc("instant_ok")
    return {"status": "ok", "task_id": task_id, "download_url": f"{API_PREFIX}/download/{task_id}/",
            "info": summarise(result, payload.kind), "error": None}


async def _mgr(method: str, *args):
    # Call the task store; Redis-backed stores block on network I/O, so they run in a worker thread.
    mgr = get_status_manager()
    fn = getattr(mgr, method)
    if getattr(mgr, "blocking", False):
        return await asyncio.to_thread(fn, *args)
    return fn(*args)


def _unknown_task() -> JSONResponse:
    return _error_response(ExtractionError(ErrorCode.TASK_NOT_FOUND))


@router.get("/extract/status/{task_id}/")
async def extract_status(task_id: str):
    if not _TASK_ID_RE.match(task_id):
        return _unknown_task()
    status = await _mgr("get_status", task_id)
    if status is None:
        return _unknown_task()
    return {"task_id": task_id, **status}


@router.get("/extract/result/{task_id}/")
async def extract_result(task_id: str):
    if not _TASK_ID_RE.match(task_id):
        return _unknown_task()
    found = await _mgr("get_result", task_id)
    if found is None:
        return _unknown_task()
    status, result = found
    state = status["status"]
    base = {"task_id": task_id, "status": state, "stage": status["stage"], "completed_at": status["completed_at"]}

    if state == TaskStatus.COMPLETED:
        return {**base, "data": result, "error": None}
    if state == TaskStatus.FAILED:
        error = status["error"] or {"code": "internal_error", "message": "Extraction failed", "retryable": False}
        try:
            http_status = ExtractionError(ErrorCode(error["code"])).http_status
        except ValueError:
            http_status = 500
        return JSONResponse(status_code=http_status, content={**base, "data": None, "error": error})
    return JSONResponse(
        status_code=202,
        content={**base, "data": None, "error": None, "message": "Result not ready yet; keep polling the status endpoint."},
        headers={"Retry-After": "1"},
    )


# Browser download (streams the file through the server)
@router.get("/download/{task_id}/")
async def download_file(task_id: str, request: Request, format_id: Optional[str] = None,
                        container: Optional[str] = None, convert: Optional[str] = None):
    # Browser download: parallel direct stream, live ffmpeg merge/convert, or yt-dlp pipe.
    from starlette.background import BackgroundTask

    from engine import downloader as dl

    if not _TASK_ID_RE.match(task_id):
        return _unknown_task()
    found = await _mgr("get_result", task_id)
    if found is None or found[1] is None:
        return _unknown_task()
    limited = _limited(request, "download")
    if limited:
        return limited
    slot = dl.acquire_slot()
    if slot is None:
        return _error_response(ExtractionError(ErrorCode.BUSY, retry_after=5))
    try:
        out = await asyncio.to_thread(
            dl.serve, found[1], format_id=format_id, container=(container or "").lower() or None,
            convert=(convert or "").lower() or None, browser_range=request.headers.get("range"))
    except ExtractionError as error:
        slot.release()
        return _error_response(error)
    except Exception as exc:  # noqa: BLE001
        slot.release()
        log.exception("download failed")
        return _error_response(classify_exception(exc))
    return StreamingResponse(dl.guarded(out["body"], slot), status_code=out["status"], headers=out["headers"],
                             media_type=out["media_type"], background=BackgroundTask(slot.release))
