# Fast browser downloads: nothing is ever stored on the server.
#
# Routes (fastest first, chosen by :func:`plan_download`):
#   direct   one stream, fetched with parallel ranged requests (several times faster
#            than a single connection; some hosts throttle single connections)
#   merge    video + audio muxed live by ffmpeg into mp4 / mkv / webm
#   convert  audio re-encoded live to mp3 / m4a / opus / ogg / wav / flac / aac
#   ytdlp    last resort: yt-dlp piped to the browser
#
# Only URLs from a finished extraction task are fetched, and every redirect hop is
# re-checked against the SSRF guard.

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import quote, urljoin

from advanced import ErrorCode, ExtractionError, get_config
from advanced.cookie_manager import cli_args, cookie_options, cookies_active
from advanced.proxy_manager import pick_proxy
from advanced import pot_provider
from advanced.stealth import impersonation_available, uses_player_clients
from advanced.url_security import is_public_media_url
from helpers import metrics

log = logging.getLogger("downflow.download")

CHUNK = 256 * 1024
RANGE_CHUNK = int(os.environ.get("DOWNFLOW_RANGE_CHUNK_MB", "4")) * 1024 * 1024
WORKERS = max(1, min(16, int(os.environ.get("DOWNFLOW_FAST_WORKERS", "6"))))
MAX_ACTIVE = int(os.environ.get("DOWNFLOW_MAX_DOWNLOADS", "8"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")

VIDEO_CONTAINERS = {"mp4": "video/mp4", "mkv": "video/x-matroska", "webm": "video/webm"}
MIME_BY_EXT = {"mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska",
               "mov": "video/quicktime", "flv": "video/x-flv", "3gp": "video/3gpp", "ts": "video/mp2t",
               "m4a": "audio/mp4", "mp3": "audio/mpeg", "opus": "audio/ogg", "ogg": "audio/ogg",
               "oga": "audio/ogg", "aac": "audio/aac", "flac": "audio/flac", "wav": "audio/wav",
               "weba": "audio/webm"}
# convert target -> (extension, mime, ffmpeg args, args when the source is already that codec)
AUDIO_TARGETS = {
    "mp3": ("mp3", "audio/mpeg", ["-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3"], None),
    "m4a": ("m4a", "audio/mp4", ["-c:a", "aac", "-b:a", "192k", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4"],
            ("mp4a", ["-c:a", "copy", "-movflags", "frag_keyframe+empty_moov", "-f", "mp4"])),
    "opus": ("opus", "audio/ogg", ["-c:a", "libopus", "-b:a", "160k", "-f", "ogg"],
             ("opus", ["-c:a", "copy", "-f", "ogg"])),
    "ogg": ("ogg", "audio/ogg", ["-c:a", "libvorbis", "-q:a", "5", "-f", "ogg"], None),
    "wav": ("wav", "audio/wav", ["-c:a", "pcm_s16le", "-f", "wav"], None),
    "flac": ("flac", "audio/flac", ["-c:a", "flac", "-f", "flac"], None),
    "aac": ("aac", "audio/aac", ["-c:a", "aac", "-b:a", "192k", "-f", "adts"],
            ("mp4a", ["-c:a", "copy", "-f", "adts"])),
}


# helpers
class Slot:
    # One download slot; release() is idempotent so every cleanup path can call it.

    def __init__(self) -> None:
        self._done = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        _slots.release()
        metrics.gauge("downloads_active", -1)


_slots = threading.BoundedSemaphore(MAX_ACTIVE)


def acquire_slot() -> Optional[Slot]:
    if not _slots.acquire(blocking=False):
        return None
    metrics.gauge("downloads_active", 1)
    metrics.inc("downloads_started")
    return Slot()


def cleanup_stale(max_age: float = 3600.0) -> None:
    for d in Path(tempfile.gettempdir()).glob("downflow-dl-*"):
        try:
            if d.is_dir() and time.time() - d.stat().st_mtime > max_age:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def safe_filename(title: Optional[str], ext: Optional[str]) -> str:
    base = re.sub(r"[^\w\s.\-()\[\]]+", "", title or "video", flags=re.U).strip()[:120] or "video"
    ext = re.sub(r"[^A-Za-z0-9]", "", ext or "")[:5] or "mp4"
    return f"{base}.{ext}"


def content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode() or "download"
    return f"attachment; filename=\"{ascii_name.replace(chr(34), '')}\"; filename*=UTF-8''{quote(filename)}"


def mime_for(ext: Optional[str], fallback: str = "application/octet-stream") -> str:
    return MIME_BY_EXT.get((ext or "").lower(), fallback)


def counted(it: Iterator[bytes]) -> Iterator[bytes]:
    for chunk in it:
        metrics.inc("bytes_sent", len(chunk))
        yield chunk


def guarded(it: Iterator[bytes], slot: Slot) -> Iterator[bytes]:
    # Yield from ``it`` and always free the slot, even if the client disconnects.
    try:
        yield from counted(it)
    finally:
        close = getattr(it, "close", None)
        if close:
            close()
        slot.release()


# HTTP (thread-local sessions for connection reuse)
_local = threading.local()


def _session():
    sess = getattr(_local, "s", None)
    if sess is None:
        if impersonation_available():
            from curl_cffi import requests as http
            sess = http.Session(impersonate="chrome")
        else:
            import requests
            sess = requests.Session()
        _local.s = sess
    return sess


def open_stream(url: str, headers: dict[str, str], range_header: Optional[str] = None,
                cache: Optional[dict] = None, proxy: Optional[str] = None):
    # GET ``url`` following redirects manually, re-checking every hop for SSRF.
    config = get_config()
    cache = cache if cache is not None else {}
    hdrs = {"User-Agent": UA, **headers}
    if range_header:
        hdrs["Range"] = range_header
    current = url
    for _ in range(6):
        if not is_public_media_url(current, config, cache):
            raise ExtractionError(ErrorCode.UNSUPPORTED_URL, detail="blocked non-public media host")
        kw = {"proxies": {"http": proxy, "https": proxy}} if proxy else {}
        resp = _session().get(current, headers=hdrs, stream=True, allow_redirects=False, timeout=(10, 60), **kw)
        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
            current = urljoin(current, resp.headers["location"])
            resp.close()
            continue
        resp.final_url = current
        return resp
    raise ExtractionError(ErrorCode.NETWORK_ERROR, detail="too many redirects")


def iter_response(resp) -> Iterator[bytes]:
    try:
        for chunk in resp.iter_content(CHUNK):
            if chunk:
                yield chunk
    finally:
        resp.close()


def _fetch_range(url: str, headers: dict, a: int, b: int, cache: dict, proxy: Optional[str] = None) -> bytes:
    last: Optional[Exception] = None
    for attempt in range(4):
        try:
            r = open_stream(url, headers, f"bytes={a}-{b}", cache, proxy)
            try:
                if r.status_code != 206 and not (r.status_code == 200 and a == 0):
                    raise IOError(f"HTTP {r.status_code}")
                data = b"".join(r.iter_content(CHUNK))
            finally:
                r.close()
            if len(data) != b - a + 1:
                raise IOError("short read")
            return data
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.25 * (attempt + 1))
    raise IOError(f"range {a}-{b} failed: {last}")


def _stream_from(url: str, headers: dict, offset: int, total: int, cache: dict, proxy: Optional[str] = None) -> Iterator[bytes]:
    # One streaming request from ``offset`` to the end. Uses a Range request, or skips bytes if the host ignores Range.
    r = open_stream(url, headers, f"bytes={offset}-" if offset else None, cache, proxy)
    skip = 0
    try:
        if r.status_code == 200 and offset:
            skip = offset
        elif r.status_code not in (200, 206):
            raise IOError(f"HTTP {r.status_code}")
        for chunk in r.iter_content(CHUNK):
            if not chunk:
                continue
            if skip:
                if len(chunk) <= skip:
                    skip -= len(chunk)
                    continue
                chunk, skip = chunk[skip:], 0
            yield chunk
    finally:
        r.close()


def parallel_iter(url: str, headers: dict, total: int, proxy: Optional[str] = None) -> Iterator[bytes]:
    # Download ``url`` with several ranged requests at once and yield the bytes in order.
    cache: dict = {}
    spans = [(s, min(s + RANGE_CHUNK, total) - 1) for s in range(0, total, RANGE_CHUNK)]
    pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="dl")
    pending: deque = deque()
    nxt = 0
    sent = 0
    try:
        while nxt < len(spans) and len(pending) < WORKERS + 2:
            pending.append(pool.submit(_fetch_range, url, headers, *spans[nxt], cache, proxy))
            nxt += 1
        while pending:
            try:
                data = pending.popleft().result()
            except Exception as exc:  # noqa: BLE001
                # The host refused a ranged request: carry on from this offset with one plain stream.
                log.warning("direct range failed at %d (%s); continuing with a single stream", sent, str(exc)[:80])
                for f in pending:
                    f.cancel()
                pending.clear()
                yield from _stream_from(url, headers, sent, total, cache, proxy)
                return
            if nxt < len(spans):
                pending.append(pool.submit(_fetch_range, url, headers, *spans[nxt], cache, proxy))
                nxt += 1
            sent += len(data)
            yield data
    finally:
        for f in pending:
            f.cancel()
        pool.shutdown(wait=False, cancel_futures=True)


def header_attempts(fmt: dict, page_url: Optional[str]) -> list[dict]:
    base = dict(fmt.get("http_headers") or {})
    out = [base]
    if page_url:
        out.append({**base, "Referer": page_url, "Origin": "/".join(page_url.split("/")[:3])})
    return out


def open_fast(fmt: dict, page_url: Optional[str], browser_range: Optional[str] = None):
    # Open a format for download. Returns (status, total_bytes|None, iterator, upstream_headers).
    #
    # Uses parallel ranged requests when the server supports them; falls back to one
    # streaming request otherwise. Raises ExtractionError if every attempt is blocked.
    url, last = fmt["download_url"], "no attempt"
    proxy = fmt.get("_proxy")
    for hdrs in header_attempts(fmt, page_url):
        try:
            if browser_range:                      # resume request from the browser: pass through
                r = open_stream(url, hdrs, browser_range, None, proxy)
                if r.status_code < 400:
                    return r.status_code, None, iter_response(r), r.headers
                last = f"HTTP {r.status_code}"
                r.close()
                continue
            probe = open_stream(url, hdrs, "bytes=0-0", None, proxy)
            if probe.status_code == 206:
                m = re.search(r"/(\d+)\s*$", probe.headers.get("content-range", ""))
                probe.close()
                total = int(m.group(1)) if m else 0
                if total > RANGE_CHUNK:
                    mid = open_stream(probe.final_url, hdrs, f"bytes={RANGE_CHUNK}-{RANGE_CHUNK}", None, proxy)
                    mid_ok = mid.status_code == 206
                    mid.close()
                    if mid_ok:
                        return 200, total, parallel_iter(probe.final_url, hdrs, total, proxy), probe.headers
                    log.info("direct host rejects ranged requests; using a single stream")
                    r = open_stream(url, hdrs, None, None, proxy)
                    return r.status_code, total, iter_response(r), r.headers
                r = open_stream(url, hdrs, None, None, proxy)
                return r.status_code, total or None, iter_response(r), r.headers
            if probe.status_code < 400:            # server ignores Range: plain stream
                return probe.status_code, int(probe.headers.get("content-length") or 0) or None, \
                    iter_response(probe), probe.headers
            last = f"HTTP {probe.status_code}"
            probe.close()
        except ExtractionError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:100]
        log.warning("direct blocked -> %s", last)
    raise ExtractionError(ErrorCode.AUTH_REQUIRED, detail=f"upstream {last}")


# planning
def _usable(f: dict, config, cache: dict) -> bool:
    return bool(f.get("download_url")) and not f.get("is_manifest") and \
        is_public_media_url(f["download_url"], config, cache)


def plan_download(data: dict, format_id: Optional[str], convert: Optional[str] = None):
    # Return ("direct", fmt) | ("merge", [video, audio]) | ("convert", fmt) | ("ytdlp", selector).
    config, cache = get_config(), {}
    formats = [f for f in data.get("formats", []) if _usable(f, config, cache)]
    streams = [s for s in (data.get("media") or {}).get("streams", []) if _usable(s, config, cache)]
    ffmpeg = bool(shutil.which("ffmpeg"))

    def best(kind: str):
        pool = [f for f in formats if f.get("kind") == kind]
        if not pool:
            return None
        if kind == "audio-only":
            return max(pool, key=lambda f: (f.get("abr") or f.get("tbr") or 0))
        return max(pool, key=lambda f: ((f.get("height") or 0), (f.get("tbr") or 0)))

    if convert in AUDIO_TARGETS and ffmpeg:
        if format_id:
            src = next((f for f in formats if f.get("format_id") == format_id), None)
        else:
            src = next((s for s in streams if s.get("role") == "audio"), None) or best("audio-only") \
                or (streams[0] if len(streams) == 1 else None) or best("video+audio")
        if src:
            return "convert", src

    if format_id:
        f = next((x for x in formats if x.get("format_id") == format_id), None)
        if f is None:
            return "ytdlp", format_id
        if f.get("kind") != "video-only":
            return "direct", f
        a = best("audio-only")
        return ("merge", [f, a]) if (ffmpeg and a) else ("direct", f)

    if len(streams) == 1:
        return "direct", streams[0]
    v = next((s for s in streams if s.get("role") == "video"), None)
    a = next((s for s in streams if s.get("role") == "audio"), None)
    if v and a and ffmpeg:
        return "merge", [v, a]
    prog = best("video+audio")
    if prog:
        return "direct", prog
    if v:
        return "direct", v
    return "ytdlp", "bv*+ba/b"


def pick_container(parts: list[dict], want: Optional[str]) -> str:
    # Honour the requested container only when both codecs fit it; otherwise use MKV (takes anything).
    vc, ac = (parts[0].get("vcodec") or "").lower(), (parts[1].get("acodec") or "").lower()
    webm_ok = vc.startswith(("vp9", "vp09", "vp8", "av01")) and ac.startswith(("opus", "vorbis"))
    mp4_ok = vc.startswith(("avc", "h264", "hev", "hvc", "av01", "vp09", "vp9")) and \
        ac.startswith(("mp4a", "aac", "mp3", "opus", "ac-3", "ec-3", "flac"))
    if want == "webm":
        return "webm" if webm_ok else "mkv"
    if want == "mp4":
        return "mp4" if mp4_ok else "mkv"
    if want == "mkv":
        return "mkv"
    if vc.startswith(("avc", "h264", "hev", "hvc")) and ac.startswith(("mp4a", "aac", "mp3")):
        return "mp4"
    return "webm" if webm_ok else "mkv"


# ffmpeg routes
def _feed(it: Iterator[bytes], sink) -> None:
    try:
        for chunk in it:
            sink.write(chunk)
    except Exception:  # noqa: BLE001 - ffmpeg closed early or upstream failed
        pass
    finally:
        try:
            sink.close()
        except Exception:  # noqa: BLE001
            pass
        close = getattr(it, "close", None)
        if close:
            close()


def _run_ffmpeg(inputs: list[Iterator[bytes]], out_args: list[str]) -> Iterator[bytes]:
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-fflags", "+genpts"]
    extra_fd = None
    if len(inputs) == 1:
        cmd = base + ["-i", "pipe:0"] + out_args
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    else:
        r, w = os.pipe()
        cmd = base + ["-i", "pipe:0", "-i", f"pipe:{r}"] + out_args
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                pass_fds=(r,))
        os.close(r)
        extra_fd = os.fdopen(w, "wb")
    feeders = [threading.Thread(target=_feed, args=(inputs[0], proc.stdin), daemon=True)]
    if extra_fd is not None:
        feeders.append(threading.Thread(target=_feed, args=(inputs[1], extra_fd), daemon=True))
    for t in feeders:
        t.start()
    try:
        while True:
            chunk = proc.stdout.read(CHUNK)
            if not chunk:
                break
            yield chunk
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for it in inputs:
            close = getattr(it, "close", None)
            if close:
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass


def open_merge(parts: list[dict], page_url: Optional[str], container: str):
    # Start a live video+audio merge. Returns (iterator, container) or None if ffmpeg produced nothing.
    for cont in dict.fromkeys([container, "mkv"]):           # fall back to mkv, which takes any codec
        flags = {"mp4": ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4"],
                 "webm": ["-f", "webm"], "mkv": ["-f", "matroska"]}[cont]
        try:
            its = [open_fast(p, page_url)[2] for p in parts]
        except ExtractionError as exc:
            log.warning("block merge -> %s", exc.detail)
            return None
        gen = _run_ffmpeg(its, ["-map", "0:v:0", "-map", "1:a:0", "-c", "copy", *flags, "pipe:1"])
        first = next(gen, None)
        if first:
            def chained(first=first, gen=gen):
                yield first
                yield from gen
            return chained(), cont
        gen.close()
        log.warning("block merge as %s -> no output", cont)
    return None


def open_convert(src: dict, page_url: Optional[str], target: str):
    # Start live audio conversion. Returns (iterator, ext, mime) or None.
    ext, mime, args, copy_rule = AUDIO_TARGETS[target]
    if copy_rule and (src.get("acodec") or "").lower().startswith(copy_rule[0]):
        args = copy_rule[1]
    it = open_fast(src, page_url)[2]
    gen = _run_ffmpeg([it], ["-vn", "-map", "0:a:0", *args, "pipe:1"])
    first = next(gen, None)
    if not first:
        gen.close()
        return None

    def chained():
        yield first
        yield from gen
    return chained(), ext, mime


# yt-dlp last resort (streamed through a pipe)
def ytdlp_stream(page_url: str, selector: str, proxy: Optional[str] = None) -> Iterator[bytes]:
    # Last resort: yt-dlp pipes the media to us. Uses the same cookies, proxy and anti-bot flags as extraction.
    config = get_config()
    sel = "b/bv*" if ("+" in selector or selector in ("bv*+ba/b", "best")) else selector
    proxy = proxy or pick_proxy(config, page_url)
    cookies = cookies_active(config, page_url)

    def pc(clients: str) -> list[str]:
        extra = [f"player_client={clients}"]
        if config.po_token:
            extra += [f"po_token={t}" for t in config.po_token]
        if config.visitor_data:
            extra.append(f"visitor_data={config.visitor_data}")
        args = ["--extractor-args", "youtube:" + ";".join(extra)]
        if config.pot_url:
            args += ["--extractor-args", f"youtubepot-bgutilhttp:base_url={config.pot_url}"]
        return args

    if uses_player_clients(page_url):
        plain: list[tuple[list[str], bool]] = []
        if config.player_clients:
            plain.append((pc(",".join(config.player_clients)), False))
        plain += [(pc("mweb,web_safari" if pot_provider.alive(config) else "web_safari"), False),
                  (pc("tv"), False), (pc("android_vr,ios"), False)]
        logged = [(pc("web_safari,web,mweb,tv"), True), (pc("tv,mweb,web_embedded"), True)] \
            if cookies and config.cookie_policy != "never" else []
        ladders = logged + plain if config.cookie_policy == "first" else plain + logged
    else:
        ladders = [([], False)]
        if cookies and config.cookie_policy != "never":
            ladders = [([], True), *ladders] if config.cookie_policy == "first" else [*ladders, ([], True)]
    if impersonation_available():
        ladders.insert(min(1, len(ladders)), (["--impersonate", "chrome"], False))
    with cookie_options(config, page_url) as copts:
        base = [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--ignore-config", "--no-playlist",
                "--no-part", "--no-cache-dir", "-N", "4", "-f", sel, "-o", "-"]
        if config.force_ipv4:
            base.append("-4")
        if config.sleep_requests:
            base += ["--sleep-requests", str(config.sleep_requests)]
        if config.sleep_interval:
            base += ["--sleep-interval", str(config.sleep_interval),
                     "--max-sleep-interval", str(max(config.max_sleep_interval, config.sleep_interval))]
        if proxy:
            base += ["--proxy", proxy]
        for n, (extra, with_cookies) in enumerate(ladders, 1):
            cmd = [*base, *(cli_args(copts) if with_cookies else []), *extra, page_url]
            log.info("fetch yt-dlp stream %d/%d", n, len(ladders))
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            first = proc.stdout.read(CHUNK)
            if not first:
                proc.kill()
                proc.wait()
                log.warning("block yt-dlp method %d/%d -> no data", n, len(ladders))
                continue
            try:
                yield first
                while True:
                    chunk = proc.stdout.read(CHUNK)
                    if not chunk:
                        break
                    yield chunk
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
            return
    raise ExtractionError(ErrorCode.AUTH_REQUIRED, detail="all download methods blocked")


# entry point used by the API
def serve(data: dict, *, format_id: Optional[str], container: Optional[str], convert: Optional[str],
          browser_range: Optional[str]) -> dict[str, Any]:
    # Blocking: pick the route, open it and return {body, status, headers, media_type}.
    page_url = data.get("webpage_url") or data.get("input_url")
    title = data.get("title")
    proxy = pick_proxy(get_config(), data.get("input_url") or page_url)   # same proxy that extracted it (IP-bound URLs)
    if proxy:
        data = {**data, "formats": [{**f, "_proxy": proxy} for f in (data.get("formats") or [])]}
    mode, plan = plan_download(data, format_id, convert)

    def reply(body, name, mime, status=200, total=None, extra=None):
        headers = {"Content-Disposition": content_disposition(name), "Cache-Control": "no-store",
                   "X-Accel-Buffering": "no", **(extra or {})}
        if total:
            headers["Content-Length"] = str(total)
        return {"body": body, "status": status, "headers": headers, "media_type": mime}

    if mode == "convert":
        got = open_convert(plan, page_url, convert)
        if got:
            body, ext, mime = got
            log.info("send  %s · convert · %s", safe_filename(title, ext), convert)
            return reply(body, safe_filename(title, ext), mime)
        log.warning("convert failed · sending original audio")
        mode, plan = "direct", plan

    if mode == "merge":
        got = open_merge(plan, page_url, pick_container(plan, container))
        if got:
            body, cont = got
            log.info("send  %s · merge · %s+%s", safe_filename(title, cont), plan[0].get("format_id"),
                     plan[1].get("format_id"))
            return reply(body, safe_filename(title, cont), VIDEO_CONTAINERS[cont])
        mode, plan = ("ytdlp", format_id or "bv*+ba/b") if page_url else ("direct", plan[0])

    if mode == "direct":
        try:
            status, total, body, up = open_fast(plan, page_url, browser_range)
        except ExtractionError:
            if not page_url:
                raise
            mode, plan = "ytdlp", (format_id or "bv*+ba/b")
        else:
            ext = plan.get("ext")
            extra = {"Accept-Ranges": up.get("accept-ranges", "none")} if browser_range else {}
            if up.get("content-range"):
                extra["Content-Range"] = up["content-range"]
            if browser_range and up.get("content-length"):
                total = None
                extra["Content-Length"] = up["content-length"]
            name = safe_filename(title, ext)
            log.info("send  %s · direct · %s%s", name, plan.get("resolution") or plan.get("format_id"),
                     f" · {total // 1048576} MB" if total else "")
            return reply(body, name, mime_for(ext, up.get("content-type", "application/octet-stream")),
                         status, total, extra)

    if not page_url:
        raise ExtractionError(ErrorCode.NO_FORMATS)
    body = ytdlp_stream(page_url, plan, proxy)
    first = next(body, None)
    if first is None:
        raise ExtractionError(ErrorCode.NO_FORMATS)
    ext = data.get("ext") or "mp4"
    log.info("send  %s · yt-dlp stream", safe_filename(title, ext))

    def chained():
        yield first
        yield from body
    return reply(chained(), safe_filename(title, ext), mime_for(ext))
