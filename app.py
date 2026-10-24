from contextlib import asynccontextmanager

import hashlib
import threading
from datetime import datetime

from fastapi import Request
import os
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from config.app_info import APP_VERSION
from config.server_config import app
from api.admin import admin as admin_router, router as auth_router
from api.apis import router, shutdown_extraction
from helpers.auth import is_authed
from dir_config import WEB_DIR, INDEX_FILE, UPDATE_CHECK_FILE, DOWNLOAD_VIDEO_FILE

app.include_router(router)
app.include_router(auth_router)
app.include_router(admin_router)

from engine.downloader import cleanup_stale
cleanup_stale()

_base_lifespan = app.router.lifespan_context


async def _cookie_watchdog():
    """Log the cookie state at startup, then re-test the cookies periodically (warns when they die)."""
    import asyncio
    import logging

    from advanced import cookie_manager, get_config
    cfg, log = get_config(), logging.getLogger("downflow.cookies")
    st = cookie_manager.cookie_status(cfg)
    if st["source"] == "file":
        log.info("cookies: file source · %s cookies · login cookies %s · expires in %s days",
                 st.get("count"), "present" if st.get("logged_in_cookies") else "MISSING", st.get("expires_in_days"))
    elif st["source"] == "browser":
        log.info("cookies: browser profile source (%s)", st.get("browser"))
    else:
        log.info("cookies: none configured (%s)", st.get("hint"))
    if st["source"] != "file" or cfg.cookie_check_interval <= 0:
        return
    await asyncio.sleep(15)
    while True:
        try:
            await asyncio.to_thread(cookie_manager.check_cookies_live, cfg)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(cfg.cookie_check_interval)


@asynccontextmanager
async def _lifespan(application):
    # Wrap the existing lifespan so running extraction tasks are stopped
    # (and reported as failed) when the application shuts down.
    import asyncio
    watchdog = asyncio.create_task(_cookie_watchdog())
    from advanced import get_config, updater
    updater.start(get_config())
    async with _base_lifespan(application) as state:
        try:
            yield state
        finally:
            watchdog.cancel()
            await shutdown_extraction()


app.router.lifespan_context = _lifespan



class _SmartGZip(GZipMiddleware):
    """Compress JSON/HTML/CSS, but never the media downloads (they are already compressed)."""

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/v1/download/"):
            return await self.app(scope, receive, send)
        await super().__call__(scope, receive, send)


app.add_middleware(_SmartGZip, minimum_size=600, compresslevel=5)

# The public website calls the API from another origin. Credentials are never allowed, so the
# cookie-protected admin area stays safe. Restrict with DOWNFLOW_CORS_ORIGINS="https://your.site,https://www.your.site".
_origins = [o.strip() for o in os.environ.get("DOWNFLOW_CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=False, allow_methods=["GET", "POST", "OPTIONS"],
                   allow_headers=["Content-Type", "Accept", "Range"],
                   expose_headers=["Content-Disposition", "Content-Length", "Content-Range", "Retry-After"], max_age=86400)

_PAGES: dict = {}      # path -> (mtime, html)
_PARTIALS = WEB_DIR / "partials"

# Header navigation, shared by every page. To change the list for one page, add an
# entry to PAGE_NAV keyed by the URL path (it replaces DEFAULT_NAV for that page).
DEFAULT_NAV = [("Home", "/"), ("Download", "/download/"), ("Version", "/check-updates"),
               ("API docs", "/docs"), ("Admin", "/admin")]
PAGE_NAV: dict[str, list[tuple[str, str]]] = {}


def _read(path) -> str:
    """Read a file, re-reading only when it changes on disk."""
    mtime = path.stat().st_mtime
    hit = _PAGES.get(path)
    if hit is None or hit[0] != mtime:
        hit = _PAGES[path] = (mtime, path.read_text(encoding="utf-8"))
    return hit[1]


def _render(path, request: Request) -> str:
    html = _read(path)
    if "<!--HEADER-->" not in html:
        return html
    here = request.url.path
    authed = is_authed(request)
    items = PAGE_NAV.get(here, DEFAULT_NAV) if authed else []
    nav = "".join(f'<a href="{href}"{" aria-current=\"page\"" if href.rstrip("/") == here.rstrip("/") else ""}>{label}</a>'
                  for label, href in items)
    nav = f'<nav class="site-nav" aria-label="Main">{nav}</nav>' if nav else ""
    user = ('<span class="chip who">saad</span><a class="btn ghost" href="/logout">Sign out</a>'
            if authed else "")
    from helpers.auth import ADMIN_USER
    user = user.replace(">saad<", f">{ADMIN_USER}<")
    header = _read(_PARTIALS / "header.html").replace("{{NAV}}", nav).replace("{{USER}}", user)
    footer = (_read(_PARTIALS / "footer.html").replace("{{YEAR}}", str(datetime.now().year))
              .replace("{{VERSION}}", APP_VERSION))
    return html.replace("<!--HEADER-->", header).replace("<!--FOOTER-->", footer)


def _page(path, request: Request) -> HTMLResponse:
    """Render an HTML page (shared header/footer injected) with ETag caching."""
    try:
        html = _render(path, request)
    except OSError:
        return HTMLResponse("<h1>Page not found.</h1>", status_code=404)
    etag = '"' + hashlib.md5(html.encode()).hexdigest()[:16] + '"'
    headers = {"Cache-Control": "private, no-cache", "ETag": etag, "X-Content-Type-Options": "nosniff",
               "X-Robots-Tag": "noindex, nofollow"}
    if request.headers.get("if-none-match") == etag:
        return HTMLResponse(status_code=304, headers=headers)
    return HTMLResponse(html, headers=headers)


def _warm_up() -> None:
    """Import the heavy extractor stack in the background so the first request is fast."""
    try:
        import yt_dlp
        yt_dlp.YoutubeDL({"quiet": True, "logger": None}).close()
        import yt_dlp.extractor  # noqa: F401
    except Exception:  # noqa: BLE001
        pass


threading.Thread(target=_warm_up, daemon=True, name="warmup").start()


def _guard(request: Request, path):
    """Every page needs the developer login; visitors are sent to the sign-in screen."""
    if not is_authed(request):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return _page(path, request)


@app.get("/", include_in_schema=False)
async def home(request: Request):
    return _guard(request, INDEX_FILE)


@app.get("/check-updates", include_in_schema=False)
async def check_updates_page(request: Request):
    return _guard(request, UPDATE_CHECK_FILE)


@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    if is_authed(request):
        return RedirectResponse("/", status_code=303)
    return _page(WEB_DIR / "login.html", request)


@app.get("/logout", include_in_schema=False)
async def logout_page():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("downflow_session", path="/")
    return resp


@app.get("/download/", include_in_schema=False)
async def download_video_page(request: Request):
    return _guard(request, DOWNLOAD_VIDEO_FILE)


@app.get("/admin", include_in_schema=False)
@app.get("/admin.html", include_in_schema=False)
async def admin_page(request: Request):
    return _guard(request, WEB_DIR / "admin.html")


@app.get("/docs", include_in_schema=False)
async def docs_page(request: Request):
    return _guard(request, WEB_DIR / "docs.html")


@app.get("/openapi.json", include_in_schema=False)
async def openapi_json(request: Request):
    if not is_authed(request):
        return JSONResponse({"detail": "Login required"}, status_code=401)
    return JSONResponse(app.openapi())


@app.get("/ui.css", include_in_schema=False)
async def ui_css():
    return FileResponse(WEB_DIR / "ui.css", media_type="text/css", headers={"Cache-Control": "public, max-age=300"})
