# Downflow backend

FastAPI service that turns a public media URL into metadata, direct links or a downloaded file. It uses yt-dlp as the extraction engine, with a layer of access strategies, per-platform cookies and a persistent database.

## Run

```
pip install -r requirements.txt
python main.py
```

Open http://127.0.0.1:8000 (site), `/docs` (API), `/admin` (console). Settings come from environment variables (prefix `DOWNFLOW_`) or the `.env` file next to `main.py`.

## Configuration files

| File | Purpose |
|---|---|
| `.env` | Your private settings (database URL and token, optional admin login, proxy). Loaded by `envfile.py` at start-up. Real environment variables win over it. |
| `envfile.py` | A tiny loader that reads `.env` into the environment. Nothing else. |
| `render.yaml`, `Dockerfile` | Render deployment (Docker image with ffmpeg, Node and the PO-token provider). There the same values are set under *Environment*. |

Turso token: line `DOWNFLOW_TURSO_TOKEN=` in `.env` (local) and the `DOWNFLOW_TURSO_TOKEN` variable in Render. Never commit it.

## Database (Turso)

Render's disk is erased on every deploy, so state lives in Turso. Without a token the server falls back to a local SQLite file (`data/downflow.db`).

* **Cookies per platform**, encrypted: Admin → Cookies. Enter a main domain (or leave it empty to detect it), a platform name (or leave it empty), alternative domains, and paste a Netscape `cookies.txt`. Save, test, disable, merge, delete at any time.
* **App updates**: Admin → Updates. Publish a version name and code, mark it forced, roll back. `/check-updates/` serves the live row.
* **Server memory**: the winning route per site and the request counters.
* The admin session secret is derived from the admin login (or `DOWNFLOW_SECRET`), so sessions survive redeploys.

Cookies are encrypted with a key from `DOWNFLOW_STORE_KEY`, else `DOWNFLOW_SECRET`, else the admin login. Keep it stable.

## How extraction works

1. The URL is validated (SSRF and redirect checks).
2. The platform is detected from the host. Routes are tried in order (several browser identities, mobile, plain, platform-specific client routes), the last winner for that site first.
3. If every cookie-less route is blocked, the cookies saved for that platform are used, and only those.
4. Blocks rotate proxies when a pool is configured; identical requests are merged; a circuit breaker stops hammering a failing site.
5. Downloads stream directly, merge through ffmpeg, or fall back to a yt-dlp pipe.

## API

| Route | Purpose |
|---|---|
| `POST /api/v1/extract/` | Start extraction, returns a task id |
| `GET /api/v1/extract/status/{id}/`, `/result/{id}/` | Progress and result |
| `POST /api/v1/instant/` | One-shot best single-file link |
| `GET /api/v1/download/{id}/` | Download through the server |
| `GET /api/v1/check-updates/` | Live app version |
| `GET /api/v1/health/` | Health |

## Project layout

```
main.py            launcher (uvicorn tuning)
app.py             pages, middleware wiring, start-up tasks
api/               public API and admin API
engine/            extraction, instant save, downloader
advanced/          strategies, cookies, database, proxies, retries, updater
helpers/           logging, auth, metrics, rate limits, task store
config/            server settings
web/               site and admin console
```

## Useful settings

`DOWNFLOW_PROXY` / `DOWNFLOW_PROXY_LIST` (use `{sid}` for sticky sessions), `DOWNFLOW_COOKIE_POLICY` (`last` default, `first`, `never`), `DOWNFLOW_AUTO_UPDATE` (`stable`, `nightly`, `off`), `DOWNFLOW_ADMIN_USER` / `DOWNFLOW_ADMIN_PASS`, `REDIS_URL` for multi-worker task state.
