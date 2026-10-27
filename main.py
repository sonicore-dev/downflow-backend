"""Entry point: ``python main.py``. Tuned uvicorn launcher (see README "Scaling")."""
import envfile  # noqa: F401  (loads .env)
import logging
import os

import uvicorn

from advanced import get_config
from config.app_info import APP_NAME
from config.server_config import app, SERVER_HOST, SERVER_PORT, FINAL_IP, PROD_URL
import app as routes  # noqa: F401  (registers routes, middleware and lifespan)


def _loop_and_http() -> dict:
    """Use uvloop / httptools when installed (requirements-prod.txt): ~2-4x the requests per second."""
    out = {}
    try:
        import uvloop  # noqa: F401
        out["loop"] = "uvloop"
    except ImportError:
        pass
    try:
        import httptools  # noqa: F401
        out["http"] = "httptools"
    except ImportError:
        pass
    return out


def _workers(cfg) -> int:
    n = cfg.workers
    if n > 1 and not cfg.redis_url:
        logging.getLogger("downflow.config").warning(
            "DOWNFLOW_WORKERS=%d needs DOWNFLOW_REDIS_URL (tasks and limits must be shared); running 1 worker", n)
        return 1
    return max(1, n)


if __name__ == "__main__":
    cfg = get_config()
    log = logging.getLogger("downflow.server")
    workers = _workers(cfg)
    extra = _loop_and_http()
    log.info("%s running at %s  (public: %s)", APP_NAME, FINAL_IP, PROD_URL)
    log.info("capacity: %d worker(s) · %d in flight · %d connections · backlog %d · loop %s · http %s · state %s",
             workers, cfg.max_inflight, cfg.max_connections, cfg.backlog, extra.get("loop", "asyncio"),
             extra.get("http", "h11"), "redis" if cfg.redis_url else "memory")
    uvicorn.run(
        "main:app", host=SERVER_HOST, port=SERVER_PORT,
        reload=os.environ.get("DOWNFLOW_RELOAD", "0") == "1",
        workers=workers if os.environ.get("DOWNFLOW_RELOAD", "0") != "1" else None,
        log_config=None, access_log=False, proxy_headers=False, server_header=False,
        limit_concurrency=cfg.max_connections, backlog=cfg.backlog, timeout_keep_alive=cfg.keepalive,
        limit_max_requests=cfg.recycle_after or None, **extra)
