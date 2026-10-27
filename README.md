# Downflow Backend

Downflow is a media-downloader backend: a **FastAPI** service that wraps **yt-dlp** with an anti-blocking layer, a task
system, an Instant Save one-click flow, a web UI and an admin dashboard. This document explains the whole backend:
how a request travels through it, every endpoint, every setting, how the logs read, how it scales, how to deploy it and how to fix it.

> Only download content you own or have permission to download. Respect each platform's terms and local law.

## Contents
1. [Quick start](#1-quick-start)
2. [Architecture](#2-architecture)
3. [Request lifecycle](#3-request-lifecycle)
4. [API reference](#4-api-reference)
5. [Instant Save](#5-instant-save)
6. [Anti-blocking layer](#6-anti-blocking-layer)
7. [Logging: reading the colours](#7-logging-reading-the-colours)
8. [Scaling and protection](#8-scaling-and-protection)
9. [Admin dashboard](#9-admin-dashboard)
10. [Configuration reference](#10-configuration-reference)
11. [Cookies, proxy and anti-bot settings](#11-cookies-proxy-and-anti-bot-settings)
12. [Deployment](#12-deployment)
13. [Security](#13-security)
14. [Troubleshooting](#14-troubleshooting)
15. [Testing and tools](#15-testing-and-tools)
16. [Project layout](#16-project-layout)

---


## Database (Turso): cookies, updates and memory that survive redeploys

Render's free disk is wiped on every deploy, so anything the server saves is stored in a Turso database instead.

**Setup (one time)**
1. Put the token in `.env` (line `DOWNFLOW_TURSO_TOKEN=PASTE_YOUR_TURSO_TOKEN_HERE`) for local/Termux runs, and in Render → *Environment* as `DOWNFLOW_TURSO_TOKEN` (plus `DOWNFLOW_TURSO_URL`) for the deployed server. Never commit it.
2. Restart. Tables are created automatically; the startup log shows `database ready · turso`.

**What is stored**
- **Cookies per platform** (encrypted): Admin → *Cookies* → add a domain (`youtube.com`, `tiktok.com`, any site), paste a Netscape `cookies.txt`, *Save & test*. Edit, disable, merge into, test and delete entries any time, no restart.
  Each request only uses the entry matching the URL's platform (aliases such as `youtu.be`, `x.com`/`twitter.com` are built in and editable), and only **after the normal cookie-less attempts failed** (`DOWNFLOW_COOKIE_POLICY=first` flips that). Cookies a site rotates during a request are saved back to the database.
  *Test* extracts your *test URL* with those cookies (best: a page/video that needs the login); without one it fetches the site and reports what it can.
- **App updates**: Admin → *Updates* → publish a version name/code (optionally force update), make an older one live, delete old ones. `/check-updates/` serves the live row (cached up to a minute).
- **Server memory**: the winning access strategy per site and request counters (flushed every 2 minutes and on shutdown).
- The admin session secret is derived from the admin login (or `DOWNFLOW_SECRET`), so you stay logged in across redeploys.

Cookies are encrypted with a key derived from `DOWNFLOW_STORE_KEY`, else `DOWNFLOW_SECRET`, else the admin login. Keep that value stable: changing it makes saved cookies unreadable (re-paste them). If the database is unreachable the server keeps working from memory and retries. Without Turso configured, a local `data/downflow.db` (SQLite) is used.

## 1. Quick start

```bash
pip install -r requirements.txt                 # core
pip install -r requirements-prod.txt            # optional: uvloop + httptools (faster) and redis (multi-worker)
cp .env.example .env                            # all optional; export what you need
DOWNFLOW_ADMIN_PASS=choose-a-password python main.py     # http://127.0.0.1:8000
```

* YouTube needs a **JavaScript runtime** (Node 20+ or Deno) and the `yt-dlp-ejs` solver (`yt-dlp[default]` includes it). `ffmpeg` is needed for merging/converting.
* `GET /admin` (sign in) → **Anti-block** tab shows engine state, proxies and strategy memory.
* Android/Termux: `sh tools/termux_setup.sh` installs everything for you.
* Docker/Render: see [Deployment](#12-deployment).

## 2. Architecture

```
 client ──► [ reverse proxy / CDN ] ──► uvicorn (1..N workers, uvloop + httptools)
                                              │
                                  ┌───────────▼────────────┐
                                  │ GuardMiddleware        │  request id · load shedding · flood guard
                                  │ (helpers/middleware.py)│  body cap · stream cap · security headers
                                  └───────────┬────────────┘  metrics · access log · JSON 500s
                                              │
                      ┌───────────────────────▼───────────────────────┐
                      │ FastAPI: pages (/, /instant/, /download/…),   │
                      │ /api/v1/* (api/apis.py), /api/v1/admin/*      │
                      └───┬──────────────┬─────────────────┬──────────┘
                          │              │                 │
              ┌───────────▼───┐   ┌──────▼────────┐  ┌─────▼───────────────────┐
              │ task store    │   │ extraction    │  │ download streaming      │
              │ memory | Redis│   │ engine        │  │ engine/downloader.py    │
              └───────────────┘   │ metadata_     │  │ direct / ffmpeg / pipe  │
                                  │ extractor.py  │  └─────────────────────────┘
                                  └──────┬────────┘
                                         │  ordered strategies, memory, hedging
                                  ┌──────▼─────────────────────────────────────┐
                                  │ advanced/: stealth · strategy_engine ·     │
                                  │ proxy_manager · pot_provider · cookies ·   │
                                  │ updater · url_security · errors   │
                                  └──────┬─────────────────────────────────────┘
                                         ▼
                                  yt-dlp  ──► the target site (optionally through a proxy)
```

Design rules that keep it fast and safe:

* **The event loop never does slow work.** Extraction and downloads run in thread pools (`asyncio.to_thread`); the loop only moves bytes and answers cheap requests. Redis calls (blocking) are also pushed to threads.
* **Everything is bounded.** Concurrent extractions, queue length, tasks kept, in-flight requests, open sockets, body size, downloads per IP, rate-limit table size, log buffer, cache size. Nothing grows with traffic.
* **Refuse early, refuse cheaply.** Overload is answered with a tiny JSON `503`/`429` before any expensive work starts.
* **Every failure has a code.** Errors are classified into stable codes (see [4.5](#45-error-codes)) with `retryable` and `Retry-After` hints.

## 3. Request lifecycle

For every HTTP request, `GuardMiddleware` runs these steps in order (cheapest first):

| # | Step | What happens | Refusal |
|---|---|---|---|
| 1 | Request ID | Reuses a sane `X-Request-ID` or creates one; all log lines of this request carry it | – |
| 2 | Load shedding | If in-flight requests ≥ `DOWNFLOW_MAX_INFLIGHT`, answer at once. `/admin`, health and auth are exempt so you can still look | `503 busy` + `Retry-After: 3` |
| 3 | Flood guard | Per-IP token bucket on `/api/` (`DOWNFLOW_API_RATE`, default 300 per 60 s) | `429 rate_limited` + `Retry-After` |
| 4 | Body cap | `Content-Length` or streamed bytes above `DOWNFLOW_MAX_BODY` | `413 payload_too_large` |
| 5 | Stream cap | More than `DOWNFLOW_MAX_IP_STREAMS` concurrent downloads for one IP | `429 rate_limited` |
| 6 | App | The route runs. An unhandled exception becomes `500 internal_error` (details only in the log) | `500` |
| 7 | Response | Adds `X-Request-ID`, `X-Response-Time`, `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, `Permissions-Policy`; records metrics and the access-log line | – |

**Client IP** (used for all per-IP limits) comes from `DOWNFLOW_CLIENT_IP_HEADER` if set (e.g. `CF-Connecting-IP`), otherwise from `X-Forwarded-For` counted `DOWNFLOW_PROXY_HOPS` entries from the **right**, so a client cannot spoof its address by sending its own header. IPv6 clients are limited per /64 network.

**Extraction flow** (`POST /api/v1/extract/`):
`validate URL (syntax, SSRF, DNS, redirects)` → `create task (queued)` → `wait for a slot (≤ QUEUE_WAIT)` → `run strategies (§6)` → `store result` → client polls `status` then reads `result`. Identical in-flight requests for the same URL share one extraction (*singleflight*) and finished ones are served from a short cache (`DOWNFLOW_CACHE_TTL`).

**Download flow** (`GET /api/v1/download/{task_id}/`): looks up the task result, acquires a download slot, then streams one of three ways: direct parallel range streaming of one media URL, a live **ffmpeg** merge/convert of separate audio+video, or a yt-dlp pipe. The proxy that extracted the link is reused, because media links are usually bound to the IP that asked for them.

## 4. API reference

Base path `/api/v1`. All responses are JSON unless noted. Errors always look like
`{"status":"error","error":{"code","message","retryable","retry_after"}}`.

### 4.1 Public endpoints

| Method & path | Purpose |
|---|---|
| `GET /api/v1/health/` | Liveness: `{"status":"success","data":{"status":"healthy","timestamp":…}}`. Never rate-limited or shed |
| `POST /api/v1/extract/` | Start extraction. Body `{"url","metadata_only":false,"format":"best"}` → **202** `{task_id,status,task,links,error}` |
| `GET /api/v1/extract/status/{task_id}/` | `{status: queued\|processing\|completed\|failed, stage, stage_label, progress, message, history, error…}` |
| `GET /api/v1/extract/result/{task_id}/` | **200** with `data` when completed · **202** (+`Retry-After: 1`) while running · the recorded error with its HTTP status when failed · **404** unknown/expired |
| `POST /api/v1/instant/` | One call: extract the single best format with retries. Body `{"url","kind":"video"\|"audio","attempt":0}` |
| `GET /api/v1/download/{task_id}/` | Stream the file. Query: `format_id`, `container`, `convert` (e.g. `mp3`). Supports `Range` |
| `GET /api/v1/check-updates/` | Version information |

`format` accepts `best`, `best_single`, `worst`, `audio`, `video` or a raw yt-dlp selector. `data.download_url` is the direct media URL when the selection is a single stream, otherwise `null` with `download_url_note`; each `formats[]` entry has its own `download_url`. Media URLs are signed and short-lived; use `/download/{task_id}/` when you want the server to deliver the file.

Typical use:

```bash
curl -s -X POST localhost:8000/api/v1/extract/ -H 'content-type: application/json' -d '{"url":"https://example.com/video"}'
curl -s localhost:8000/api/v1/extract/status/<task_id>/
curl -s localhost:8000/api/v1/extract/result/<task_id>/
curl -OJ localhost:8000/api/v1/download/<task_id>/?format_id=18
```

### 4.2 Pages

`/` home · `/instant/` Instant Save · `/download/` Proper Save · `/check-updates` version · `/docs` OpenAPI UI · `/login` · `/admin`.

### 4.3 Auth and admin endpoints (cookie session; `401` otherwise)

| Method & path | Purpose |
|---|---|
| `POST /api/v1/auth/login` · `POST /auth/logout` · `GET /auth/me` | Session handling |
| `GET /api/v1/admin/stats` | Counters, **traffic** (rps, p50/p95/p99, status classes, 60 s series), **server** (in-flight, peak, shed, limited, streams, state memory/redis), system, tools |
| `GET /api/v1/admin/logs?after=<id>&limit=500` · `POST …/logs/clear` | Log ring buffer (incremental) |
| `GET /api/v1/admin/tasks` · `GET …/config` · `GET …/routes` | Recent tasks · effective configuration · route list for the API tester |
| `GET /api/v1/admin/antiblock` | Doctor checks, proxy health, PO provider, strategy memory, yt-dlp version |
| `POST /api/v1/admin/update-ytdlp` | Upgrade yt-dlp now (`{"channel":"stable"\|"nightly"}`) |
| `GET/POST /api/v1/admin/cookies` · `POST …/cookies/check` | Cookie status (never values) · hot-replace the cookie file · live validity test |

### 4.4 Response headers

`X-Request-ID` (quote it when reporting a problem: it is in every related log line), `X-Response-Time`, `Retry-After` on `429/503`, plus the security headers listed above.

### 4.5 Error codes

| Code | HTTP | Retry? | Meaning |
|---|---|---|---|
| `invalid_url` · `url_not_allowed` | 400 | no | Malformed URL · blocked by the SSRF guard |
| `unsupported_url` · `no_formats` · `no_media_url` · `format_not_available` | 422 | no | No extractor / nothing downloadable / chosen format missing |
| `rate_limited` | 429 | yes | You or the site rate-limited this request |
| `auth_required` · `cookies_invalid` · `private_video` · `geo_restricted` · `age_restricted` | 403 | no | Needs login / valid cookies / different region |
| `ip_blocked` | 503 | yes | Several routes in a row were blocked: **the server's IP is flagged** (add a proxy, see §6) |
| `unavailable` · `task_not_found` | 404 | no | Removed content · unknown/expired task |
| `timeout` · `network_error` | 504 / 502 | yes | Upstream slow/unreachable |
| `extractor_outdated` | 502 | no | Update yt-dlp |
| `busy` · `cancelled` | 503 | yes | Server at capacity / shutting down |
| `config_error` · `internal_error` | 500 | no | Server-side problem (see logs by request id) |

## 5. Instant Save

Web tab **Instant Save** (`/instant/`): one big circle button reads the copied link (or accepts a paste), asks for **one** best format only and starts the browser download without a format picker. **Proper Save** (`/download/`) is the full flow with format choice.

* `POST /api/v1/instant/` returns `{"status":"ok","task_id","download_url","info":{title,uploader,duration,thumbnail,platform,kind,ext,resolution,merged}}`; then `GET download_url` streams the file.
* Speed: single-video mode (never walks a playlist), a selector that prefers one pre-merged file (`DOWNFLOW_INSTANT_VIDEO_FORMAT`, default `b/bv*+ba/b`; audio `ba/b`), the strategy memory tries the last working route first, repeat links come from cache.
* Retries: up to `DOWNFLOW_INSTANT_ATTEMPTS` (3) inside one `DOWNFLOW_INSTANT_BUDGET` (45 s); each retry uses a fresh route/proxy session; a block from the only IP is not retried (that only prolongs the block). The page also retries (3 starts) if the browser download itself fails.
* Code: `engine/instant.py`, `api/apis.py`, `web/instant_save.html`.

## 6. Anti-blocking layer

Most "403 / confirm you're not a bot" errors are caused by **IP reputation**, the TLS/HTTP2 fingerprint and the player client in use, not by a missing login. Downflow therefore tries *several different routes* and learns which one works.

| Piece | File | What it does |
|---|---|---|
| Strategies | `advanced/stealth.py` | Ordered yt-dlp option sets: browser TLS impersonation (curl_cffi chrome/safari/edge/firefox), YouTube player clients (`web_safari`, `mweb`+PO token, `tv`, `android_vr` without the watch page, embedded, ios…), then cookie routes, then a rotating-header last resort. Other sites get a chrome → safari → edge → firefox → plain ladder |
| Engine | `advanced/strategy_engine.py` | Runs the ladder with **memory** (the route that worked last time goes first for 10 min of failures), **hedging** (start the next route after `HEDGE_DELAY` s on another exit), **instant failover** on a clear block, and a **circuit breaker** (after 4 blocks from one IP it stops hammering it, tries the signed-in routes once, then reports `ip_blocked`) |
| Proxies | `advanced/proxy_manager.py` | Pool with health, per-site cool-down, sticky sessions (`{sid}` per video, `{rand}` always new), affinity so the download uses the same exit IP |
| PO tokens | `advanced/pot_provider.py` | Detects the bgutil provider server (`:4416`) and puts PO-token routes first when it is up |
| Cookies | `advanced/cookie_manager.py` | Hot-reloaded cookie file, write-back of rotated cookies, live validity checks. Policy `last` keeps your account away from datacentre traffic |
| Updater | `advanced/updater.py` | Optional background `yt-dlp` upgrade (`AUTO_UPDATE=stable`), cross-process lock so only one worker runs pip |

**Honest limits.** No software can make a *flagged IP* trusted. If the log says "proxy pool: 0" and every route returns blocks/`ip_blocked`, the fix is a clean exit IP: a residential/mobile proxy (`DOWNFLOW_PROXY=http://user-session-{sid}:pass@gate:7000`) and/or signed-in cookies. Private, paid and members-only content always needs real credentials.

## 7. Logging: reading the colours

Console format (one line per event):

```
14:03:22.418  INF  strategy   a1b2c3  try   yt-tv 2/9 · proxy none
time(ms)      lvl  component  req id  message
```

Every colour carries meaning, and the admin **Logs** terminal uses the same palette (press **Legend** in the terminal).

**Level badge:** `DBG` grey (detail) · `INF` green (normal) · `WRN` amber (recoverable) · `ERR` red (failure) · `CRT` magenta (cannot continue).

**Component** (who spoke): `access` blue (HTTP) · `api` pink · `engine` cyan · `strategy` violet (route ladder) · `proxy` orange · `instant` lime · `download` gold · `retry` sand · `limit`/`guard` yellow/red (protection) · `cookies` tan · `doctor` teal · `update` green · `redis` brick · `config` slate-blue · `server` light blue (uvicorn/lifecycle).

**Message tag** (first word): `start` cyan · `try` blue · `ok` / `done` green · `block` amber · `fail` red · `send` pink · `fetch` light blue · `direct` orange · `merge` violet · `hedge` violet · `retry` sand · `queue` yellow · `shed` bright red · `limit` yellow · `giving` red · `check` teal · `cache` green.

**Inside a message:** URLs blue underlined · numbers with units (`1.2s`, `45MB`, `80%`) bold yellow · `n/m` counters purple · `→` red · error codes (`auth_required`, `ip_blocked`…) red.

**HTTP access lines:** method (`GET` green, `POST` amber, `DELETE` red, others blue/violet) · status by class (**2xx green, 3xx cyan, 4xx amber, 5xx red**) · latency (**<250 ms green, <1 s white, <3 s amber, slower red**) · size and client IP grey · `slow` / `client closed` notes in yellow. The 6-character **request id** (pink-violet) is the same on every line of one request.

**Noise control:** polling endpoints (`/extract/status`, `/admin/*`, health, static files) are logged at `DEBUG` only; identical consecutive lines collapse into "previous line repeated N more times"; above `DOWNFLOW_ACCESS_LOG_RPS` (50 req/s) only failures and slow requests are logged individually, with a 10-second `traffic …` summary line; guard warnings (`shed`, `limit`) are throttled to one per key per 5 s (exact totals stay in `/admin/stats`).

**Formats:** `DOWNFLOW_LOG_FORMAT=pretty` (default, coloured on a terminal) · `plain` (no colour) · `json` (one object per line: `ts, level, component, rid, msg, http`) for Loki/Datadog/CloudWatch. `DOWNFLOW_COLOR=1` forces colour (the Docker image sets it so Render's log viewer shows it), `DOWNFLOW_COLOR=0` or `NO_COLOR` disables it. `DOWNFLOW_LOG_LEVEL=DEBUG` shows everything.

## 8. Scaling and protection

### 8.1 What "millions of requests" means in practice

One machine cannot hold millions of *simultaneous* open connections or run millions of extractions at once, and no honest design promises that. What Downflow guarantees is that **overload never turns into a crash or a pile-up**: work is classified by cost, cheap work is served at very high rate, expensive work is bounded and queued, and everything beyond capacity is refused in microseconds with a clear retry hint. Then you add capacity horizontally.

| Traffic | Cost | How it is handled |
|---|---|---|
| Pages, static files, health, status polls | tiny | Served straight from the event loop (ETag cached HTML). Measured here: **~4,000–8,000 req/s per single process** on a small test VM, p99 under 200 ms at 500 concurrent connections (your hardware will differ; use `tools/loadtest.py`) |
| Rejected traffic (rate-limited / shed) | tiny | Answered by the middleware before any routing: thousands per second per core |
| `POST /extract`, `/instant` | **expensive** (seconds of yt-dlp work, outbound network) | Bounded by `MAX_CONCURRENT` running + `MAX_QUEUED` waiting + `QUEUE_WAIT`; beyond that → `503 busy` with `Retry-After` |
| `GET /download` | **bandwidth-bound** | Download slots + per-IP stream cap; streaming is zero-copy chunks, never buffered |

The real ceilings for the expensive work are your CPU/RAM, outbound bandwidth and, above all, **the target site's tolerance of your IPs**, not the web framework.

### 8.2 The protection layers (outside in)

1. **OS / uvicorn:** `DOWNFLOW_BACKLOG` (accept queue, 4096), `DOWNFLOW_MAX_CONNECTIONS` (sockets per worker, uvicorn answers 503 above it), `DOWNFLOW_KEEPALIVE` (idle sockets released after 5 s), optional `DOWNFLOW_RECYCLE_AFTER` (restart a worker after N requests to cap slow leaks). Access logging by uvicorn is off (the middleware logs instead); `uvloop` + `httptools` are used automatically when installed.
2. **Load shedding:** `DOWNFLOW_MAX_INFLIGHT` (2000) concurrent requests per worker; the 2001st gets `503 busy` immediately. Admin/health are exempt.
3. **Flood guard:** token bucket per client (`DOWNFLOW_API_RATE=300/60`: bursts up to 300, refilling 5/s). Memory is bounded (200,000 tracked clients, LRU) so spoofed addresses cannot exhaust RAM.
4. **Route limits:** `DOWNFLOW_RL_EXTRACT` and `DOWNFLOW_RL_DOWNLOAD` (default `20/60`) on the expensive routes; signed-in admins are exempt.
5. **Body / stream caps:** `DOWNFLOW_MAX_BODY` (64 KB) and `DOWNFLOW_MAX_IP_STREAMS` (4).
6. **Work queue:** `MAX_CONCURRENT`, `MAX_QUEUED`, `QUEUE_WAIT`, download slots.
7. **Dedup + cache:** singleflight collapses identical simultaneous extractions into one; results are cached `CACHE_TTL` seconds, so a viral link costs one extraction, not a million.
8. **Upstream protection:** the anti-block layer's circuit breaker and proxy cool-downs stop a flagged IP from being hammered.

### 8.3 Scaling out

* **Vertical (one box):** `pip install -r requirements-prod.txt`, raise `DOWNFLOW_MAX_CONCURRENT` to what your CPU/bandwidth allow (each extraction is mostly waiting on the network; 8–32 is common on 2–4 vCPUs).
* **Multiple workers** (use all cores): set `DOWNFLOW_WORKERS=4` (or `auto`) **and** `DOWNFLOW_REDIS_URL`. Workers are separate processes, so the task store and rate limits must be shared; the server refuses to start extra workers without Redis (it logs a warning and runs 1). With Redis, a task created by one worker is readable by any other, and per-IP limits are global.
* **Multiple instances** (horizontal): run N containers behind a load balancer, all pointing at the same Redis. Any instance can answer any `status/result` call. `download` streams need no stickiness because the task result lives in Redis.
* **Put a CDN/reverse proxy in front** (Cloudflare, nginx, Render's edge): it absorbs connection floods and TLS cost. Set `DOWNFLOW_PROXY_HOPS` to the number of proxies (Render/Cloudflare alone = 1) or `DOWNFLOW_CLIENT_IP_HEADER=CF-Connecting-IP`, otherwise every visitor looks like one IP.
* **Capacity maths:** extraction throughput ≈ `MAX_CONCURRENT × workers × instances ÷ average extraction seconds`. Example: 16 slots × 4 workers ÷ 4 s ≈ 16 extractions/s ≈ 1.4 M/day, cache hits not counted. Your proxy pool and the target site's limits cap this long before the CPU does.
* **Redis failure:** rate limiting falls back to local buckets automatically; the task store reports errors per request rather than crashing the server.

### 8.4 Observing load

Admin **Overview** shows requests/s (60 s bars), p50/p95/p99 latency, in-flight vs limit, shed/limited counters, 2xx/4xx/5xx, state backend and workers. `GET /api/v1/admin/stats` returns the same as JSON. Load test it yourself: `python tools/loadtest.py http://127.0.0.1:8000/ 30000 500`.

## 9. Admin dashboard

`/admin` (sign in). Tabs: **Overview** (traffic, throughput, system, capabilities, platforms) · **Logs** · **API Tester** · **Tasks** · **Cookies** · **Anti-block** · **Config**. Polling pauses when the tab is hidden and backs off when the server is unreachable.

**Logs terminal:** level chips (DBG/INF/WRN/ERR, coloured like the server), live search (`/` focuses it, 120 ms debounce), component filter, **font size clamped to 10–20 px** (A−/A+, Ctrl+wheel, two-finger pinch; the limit is enforced both in JavaScript and in CSS `clamp()`, so no setting can make it unreadable or huge; click the size to reset; saved per device), Wrap toggle (horizontal scroll when off), Follow (auto-scroll; scrolling up pauses it and shows a **↓ New logs** button), Pause, Legend, Copy, Save `.txt`, Clear. Rendering is batched with `requestAnimationFrame` and capped at 1,000 DOM lines so it stays smooth for hours; HTTP lines are drawn from structured data (method/status/latency colours).

**Anti-block tab:** doctor checklist with fixes, engine settings, PO provider state, yt-dlp version + **Update now**, proxy pool health table, strategy memory (winners and failing routes).

## 10. Configuration reference

Every setting is an environment variable prefixed `DOWNFLOW_`; values are range-checked at start-up and bad values fall back to the default. Table generated from `advanced/extractor_config.py`; `.env.example` has a ready-to-edit template.

**Extraction & retries**

| Variable | Default | Meaning |
|---|---|---|
| `DOWNFLOW_SOCKET_TIMEOUT` | `20.0` | seconds a single network read may stall |
| `DOWNFLOW_REQUEST_TIMEOUT` | `60.0` | overall per-request budget (API + retries) |
| `DOWNFLOW_EXTRACTOR_RETRIES` | `2` | yt-dlp internal extractor retries |
| `DOWNFLOW_DOWNLOAD_RETRIES` | `2` | yt-dlp "retries" |
| `DOWNFLOW_FRAGMENT_RETRIES` | `2` | yt-dlp retries per media fragment |
| `DOWNFLOW_RETRY_MAX_ATTEMPTS` | `3` | engine-level attempts for transient errors |
| `DOWNFLOW_RETRY_BASE_DELAY` | `1.5` | first backoff delay (s) |
| `DOWNFLOW_RETRY_MAX_DELAY` | `20.0` | backoff cap (s) |
| `DOWNFLOW_RETRY_RATE_LIMIT_ATTEMPTS` | `2` | attempts after a rate-limit answer |
| `DOWNFLOW_MAX_CONCURRENT` | `4` | extractions running in parallel per process (thread pool + semaphore) |
| `DOWNFLOW_QUEUE_WAIT` | `30.0` | seconds a queued task waits for a free slot |
| `DOWNFLOW_MAX_QUEUED` | `50` | tasks allowed to wait for a slot |
| `DOWNFLOW_TASK_TTL` | `900.0` | seconds finished tasks stay retrievable |
| `DOWNFLOW_MAX_TASKS` | `500` | hard cap on tracked tasks |
| `DOWNFLOW_MAX_URL_LENGTH` | `2048` | longest accepted URL |
| `DOWNFLOW_MAX_PLAYLIST_ENTRIES` | `50` | Formats |
| `DOWNFLOW_DEFAULT_FORMAT` | `best` | format used when the request omits one |
| `DOWNFLOW_MAX_FORMAT_LENGTH` | `200` | Proxy / cookies |

**Proxy**

| Variable | Default | Meaning |
|---|---|---|
| `DOWNFLOW_PROXY` | – | single proxy URL (http/https/socks5h); supports `{sid}` / `{rand}` |
| `DOWNFLOW_PROXY_LIST` | `()` | optional pool (rotating / residential); see advanced/proxy_manager.py |
| `DOWNFLOW_PROXY_MODE` | `sticky` | sticky (same proxy per video) \| round_robin \| random \| weighted |
| `DOWNFLOW_PROXY_COOLDOWN` | `90.0` | base seconds a blocked/dead proxy is parked (doubles per failure) |
| `DOWNFLOW_PROXY_AFFINITY_TTL` | `7200.0` | remember which proxy served a URL (IP-bound media links) |
| `DOWNFLOW_PROXY_DIRECT_FALLBACK` | `1` | connect directly if every configured proxy is unusable |

**Cookies**

| Variable | Default | Meaning |
|---|---|---|
| `DOWNFLOW_COOKIE_FILE` | – | explicit path; default is <cookies_dir>/youtube.txt |
| `DOWNFLOW_COOKIES_DIR` | `<app>/cookies` | folder holding `youtube.txt` (default `<app>/cookies`) |
| `DOWNFLOW_COOKIES_FROM_BROWSER` | – | yt-dlp spec: BROWSER[+KEYRING][:PROFILE][::CONTAINER] |
| `DOWNFLOW_BROWSER_PROFILE_DIR` | – | persistent Chrome/Chromium --user-data-dir |
| `DOWNFLOW_BROWSER_NAME` | `chromium` | browser for profile cookies |
| `DOWNFLOW_BROWSER_KEYRING` | `basictext` | keyring used to decrypt profile cookies |
| `DOWNFLOW_COOKIE_SOURCE` | `auto` | auto \| file \| browser \| none |
| `DOWNFLOW_COOKIE_WRITEBACK` | `1` | keep rotated cookies yt-dlp refreshes during use |
| `DOWNFLOW_COOKIE_CHECK_INTERVAL` | `3600.0` | seconds between live validity checks (0 = off) |
| `DOWNFLOW_COOKIE_POLICY` | `last` | last = cookie-less routes first \| first \| never |

**Anti-block behaviour**

| Variable | Default | Meaning |
|---|---|---|
| `DOWNFLOW_FORCE_IPV4` | `1` | IPv4 only (many flagged IPv6 ranges) |
| `DOWNFLOW_SLEEP_REQUESTS` | `0.5` | pause between extractor requests (seconds) |
| `DOWNFLOW_SLEEP_INTERVAL` | `0.0` | pause before each download (yt-dlp --sleep-interval) |
| `DOWNFLOW_MAX_SLEEP_INTERVAL` | `0.0` | upper bound for random download sleep |
| `DOWNFLOW_YT_PLAYER_CLIENTS` | `()` | empty = automatic (cookie-friendly clients when cookies are active) |
| `DOWNFLOW_YT_PO_TOKEN` | `()` | e.g. "web.gvs+XXXX" (see yt-dlp PO Token guide) |
| `DOWNFLOW_YT_VISITOR_DATA` | – | visitor data paired with a manual PO token |
| `DOWNFLOW_POT_URL` | – | bgutil PO-token provider server (default http://127.0.0.1:4416) |
| `DOWNFLOW_RACE_WIDTH` | `2` | max strategies running in parallel (1 = strictly sequential) |
| `DOWNFLOW_HEDGE_DELAY` | `5.0` | start the next strategy in parallel after this many seconds (0 = off) |
| `DOWNFLOW_CACHE_TTL` | `900.0` | seconds an identical extraction is served from memory |
| `DOWNFLOW_CACHE_MAX` | `256` | entries kept in the extraction cache (LRU) |
| `DOWNFLOW_AUTO_UPDATE` | `off` | off \| stable \| nightly -- upgrade yt-dlp in the background |
| `DOWNFLOW_UPDATE_INTERVAL` | `21600.0` | seconds between update checks |
| `DOWNFLOW_ROTATE_USER_AGENT` | `0` | random User-Agent per request |
| `DOWNFLOW_USER_AGENT_FILE` | `advanced/headers/user-agents.txt` | list of User-Agents to rotate |
| `DOWNFLOW_ACCEPT_LANGUAGE` | `en-US,en;q=0.9` | Security |
| `DOWNFLOW_JS_RUNTIMES` | `deno,node` | JavaScript runtimes yt-dlp may use for YouTube challenges |
| `DOWNFLOW_REMOTE_COMPONENTS` | `()` | e.g. "ejs:github" to allow solver download |

**Instant Save**

| Variable | Default | Meaning |
|---|---|---|
| `DOWNFLOW_INSTANT_VIDEO_FORMAT` | `b/bv*+ba/b` | Instant Save: best single file first (no merge), merge only if needed |
| `DOWNFLOW_INSTANT_AUDIO_FORMAT` | `ba/b` | Instant Save audio selector |
| `DOWNFLOW_INSTANT_ATTEMPTS` | `3` | extraction attempts for one Instant Save click |
| `DOWNFLOW_INSTANT_BUDGET` | `45.0` | total seconds for all attempts |

**Scale & protection**

| Variable | Default | Meaning |
|---|---|---|
| `DOWNFLOW_WORKERS` | `1` | uvicorn worker processes (needs DOWNFLOW_REDIS_URL when > 1) |
| `DOWNFLOW_REDIS_URL` | – | shared task store + rate limits for multi-worker / multi-instance setups |
| `DOWNFLOW_MAX_INFLIGHT` | `2000` | requests being handled at once before new ones get a fast 503 |
| `DOWNFLOW_MAX_CONNECTIONS` | `10000` | open sockets per worker (uvicorn limit_concurrency), 0 = unlimited |
| `DOWNFLOW_BACKLOG` | `4096` | pending TCP connections the OS may queue |
| `DOWNFLOW_KEEPALIVE` | `5` | seconds an idle keep-alive connection is kept |
| `DOWNFLOW_RECYCLE_AFTER` | `0` | restart a worker after N requests (0 = never; guards slow leaks) |
| `DOWNFLOW_API_RATE` | `300/60` | per-IP requests/window for all /api/ routes (token bucket) |
| `DOWNFLOW_MAX_BODY` | `65536` | bytes accepted in a public API request body |
| `DOWNFLOW_MAX_IP_STREAMS` | `4` | simultaneous file downloads per client IP |
| `DOWNFLOW_PROXY_HOPS` | `1` | trusted reverse proxies in front of the app (X-Forwarded-For from the right) |
| `DOWNFLOW_CLIENT_IP_HEADER` | – | e.g. CF-Connecting-IP; overrides proxy_hops when present |
| `DOWNFLOW_ACCESS_LOG_RPS` | `50` | above this many requests/s only slow/failed ones are logged, plus a summary |
| `DOWNFLOW_SLOW_REQUEST` | `3.0` | seconds after which a request is flagged slow in the log |

**URL safety (SSRF guard)**

| Variable | Default | Meaning |
|---|---|---|
| `DOWNFLOW_ALLOWED_PORTS` | `80,443` | ports a submitted URL may use |
| `DOWNFLOW_ALLOW_PRIVATE_HOSTS` | `0` | SSRF guard override (development only) |
| `DOWNFLOW_CHECK_REDIRECTS` | `1` | validate every hop of the redirect chain |
| `DOWNFLOW_MAX_REDIRECTS` | `5` | hops followed while validating |
| `DOWNFLOW_REDIRECT_CHECK_TIMEOUT` | `8.0` | yt-dlp |

**Misc**

| Variable | Default | Meaning |
|---|---|---|
| `DOWNFLOW_VERBOSE` | `0` | yt-dlp debug output |

**Other variables:** `PORT` (listen port; `DOWNFLOW_` prefix not used) · `DOWNFLOW_ADMIN_USER`, `DOWNFLOW_ADMIN_PASS`, `DOWNFLOW_SECRET` (admin login and session signing) · `DOWNFLOW_CORS_ORIGINS` (comma list, default `*`) · `DOWNFLOW_RL_EXTRACT`, `DOWNFLOW_RL_DOWNLOAD`, `DOWNFLOW_RL_API` (`count/seconds` per-route limits) · `DOWNFLOW_MAX_DOWNLOADS`, `DOWNFLOW_FAST_WORKERS`, `DOWNFLOW_RANGE_CHUNK_MB` (download engine) · `DOWNFLOW_LOG_LEVEL`, `DOWNFLOW_LOG_FORMAT`, `DOWNFLOW_COLOR`, `DOWNFLOW_LOG_BUFFER` · `DOWNFLOW_RELOAD=1` (dev auto-reload) · `DOWNFLOW_POT_SERVER`, `DOWNFLOW_POT_PORT` (start.sh).


## 11. Cookies, proxy and anti-bot settings

### YouTube cookies
* The cookie file lives at `cookies/youtube.txt` (Netscape format, `chmod 600`, git-ignored). Change the folder with
  `DOWNFLOW_COOKIES_DIR` (for example `/app/cookies`) or the exact file with `DOWNFLOW_COOKIE_FILE`.
* **Hot reload:** the file is read on every extraction. Replace it on disk, or paste new text in the admin
  dashboard (**Cookies** tab), or `POST /api/v1/admin/cookies {"text": "..."}`. No restart needed.
* **Validation:** the admin **Cookies** tab (and `POST /api/v1/admin/cookies/check`) asks YouTube whether the cookies are
  still signed in. The server also tests them every `DOWNFLOW_COOKIE_CHECK_INTERVAL` seconds (default 3600) and logs a
  warning when they die. `GET /api/v1/admin/cookies` shows count, login cookies and days until expiry (never values).
* **Auto-refresh:** YouTube rotates some cookies while they are used. With `DOWNFLOW_COOKIE_WRITEBACK=1` (default) the
  rotated cookies are saved back atomically, which keeps the session alive much longer.
* Switch source with `DOWNFLOW_COOKIE_SOURCE=auto|file|browser|none` (auto = file if present, else browser profile).

### Persistent browser profile (for automatic refresh later)
1. `tools/browser_profile.sh` opens Chromium with `--user-data-dir=./browser-profile --password-store=basic`.
   Sign in to YouTube once, then close the browser.
2. Set `DOWNFLOW_BROWSER_PROFILE_DIR=/path/to/browser-profile` (uses `chromium+basictext`), or the full yt-dlp spec
   `DOWNFLOW_COOKIES_FROM_BROWSER=chromium+basictext:/path/to/browser-profile`.
3. `python tools/refresh_cookies.py` exports fresh cookies from that profile into `cookies/youtube.txt` (cron-friendly).

### Proxy (residential / rotating)
`DOWNFLOW_PROXY=http://user:pass@host:port` (also `socks5h://`). For a pool use `DOWNFLOW_PROXY_LIST=a,b,c` or
`DOWNFLOW_PROXY_FILE`, with `DOWNFLOW_PROXY_MODE=sticky` (default: one video always uses the same proxy, required because
YouTube media links are IP-bound), `round_robin` or `random`. The same proxy is used for extraction and for the download.

### Anti-bot defaults
IPv4 only (`DOWNFLOW_FORCE_IPV4=1`), 0.5 s between extractor requests (`DOWNFLOW_SLEEP_REQUESTS`), optional
`DOWNFLOW_SLEEP_INTERVAL`. With cookies active, YouTube uses cookie-friendly player clients (`web_safari, web, mweb, tv`);
override with `DOWNFLOW_YT_PLAYER_CLIENTS`, and add a PO token / visitor data with `DOWNFLOW_YT_PO_TOKEN`,
`DOWNFLOW_YT_VISITOR_DATA`. See `.env.example` for everything.

**Security:** the cookies are the keys to your Google account. Keep `cookies/` private, never commit or share it, and
change the admin password (`DOWNFLOW_ADMIN_PASS`).


## 12. Deployment

**Render (recommended: Docker).** `render.yaml` defines a Docker web service. The image (`Dockerfile`) builds the bgutil PO-token server, installs ffmpeg, Node (also the JS runtime for yt-dlp) and the Python dependencies including `requirements-prod.txt`, then `start.sh` launches the provider in the background and `python main.py` in the foreground. Set in the dashboard: `DOWNFLOW_ADMIN_USER`, `DOWNFLOW_ADMIN_PASS`, `DOWNFLOW_PROXY` (residential, `{sid}`), and the cookie file as a Secret File at `/etc/secrets/youtube.txt`. Free instances sleep and are easily flagged; use a paid plan. Render sits behind one proxy, so the default `DOWNFLOW_PROXY_HOPS=1` is right. For several instances add a Redis (Render Key Value) and set `DOWNFLOW_REDIS_URL`.

**Docker anywhere.** `docker build -t downflow . && docker run -p 8000:8000 -e DOWNFLOW_ADMIN_PASS=… -e DOWNFLOW_PROXY_HOPS=0 -v $PWD/cookies:/app/cookies downflow` (use `PROXY_HOPS=0` when nothing sits in front).

**Bare metal / VM.** `pip install -r requirements.txt -r requirements-prod.txt`, install ffmpeg and Node 22, run under systemd: `DOWNFLOW_WORKERS=auto DOWNFLOW_REDIS_URL=redis://… python main.py` behind nginx or Cloudflare.

**Termux (Android).** `sh tools/termux_setup.sh` installs Python, Node, ffmpeg and the packages; then `python main.py`. Mobile-carrier IPs are often flagged by YouTube; toggling airplane mode for a new IP, or using a proxy, helps.

**Shutdown.** On SIGTERM running extractions are failed with `cancelled` so clients get an answer; in-flight downloads end when the client disconnects.

## 13. Security

* **Change the admin credentials.** If `DOWNFLOW_ADMIN_USER` / `DOWNFLOW_ADMIN_PASS` are unset the code falls back to built-in defaults, which are public in the source. Always set both plus `DOWNFLOW_SECRET` (a long random string) in production; without a fixed secret, sessions do not survive restarts or work across workers.
* **SSRF guard** (`advanced/url_security.py`): only http/https on allowed ports, DNS results and every redirect hop must be public addresses, returned media hosts are checked too. `DOWNFLOW_ALLOW_PRIVATE_HOSTS=1` is for development only. yt-dlp resolves DNS itself, so also apply egress filtering where possible.
* **Cookies are keys to your account.** Keep `cookies/` private (git-ignored, `chmod 600`), never expose it; the admin API never returns cookie values; proxy credentials are redacted in logs and the admin UI.
* **Rate limits cannot be dodged** with a forged `X-Forwarded-For` (hop-counted from the right) or by rotating IPv6 addresses (/64 grouping).
* **Errors never leak internals:** unhandled exceptions return a generic JSON 500; the stack trace stays in the server log, tied to the `X-Request-ID`.
* **CORS** defaults to `*` without credentials; restrict with `DOWNFLOW_CORS_ORIGINS=https://your.site`.
* Security headers are added to every response by the middleware.

## 14. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| YouTube: `ip_blocked`, "confirm you're not a bot", 429 on the watch page | The server IP is flagged | Admin → Anti-block → doctor. Use a residential/mobile proxy (`DOWNFLOW_PROXY=…{sid}…`), load cookies (`cookies/youtube.txt`), try another network; keep requests few |
| "Requested format is not available" / only images | Blocked session served stripped formats, or no JS runtime/solver | Install Node/Deno, `pip install -U "yt-dlp[default]"`, run the doctor |
| `auth_required` on one site | Needs login | Provide cookies for that site |
| 503 `busy` | Queue full or load shedding | Raise `MAX_CONCURRENT` / `MAX_QUEUED`, add workers + Redis, or retry after `Retry-After` |
| 429 for every user | All users share one IP in the limiter | Set `DOWNFLOW_PROXY_HOPS` / `CLIENT_IP_HEADER` correctly |
| Task 404 on status with several workers | Per-process memory store | Set `DOWNFLOW_REDIS_URL` |
| Logs have no colour in Docker | Not a TTY | `DOWNFLOW_COLOR=1` |
| Extraction broke after a site change | yt-dlp outdated | `DOWNFLOW_AUTO_UPDATE=stable` or Admin → Anti-block → Update now |
| Need one request's whole story | – | Search the request id (from the `X-Request-ID` header) in the Logs terminal |

## 15. Testing and tools

* `python -m pytest -q tests` — 39 offline tests: anti-block engine (routing, hedging, circuit breaker, singleflight, summary codes, strategy memory), logging formats, client-IP spoof resistance, token bucket and memory bound, Redis limiter and task store (fakeredis), and the middleware (request id, security headers, load shedding, flood guard, body cap, JSON 500, per-IP stream cap).
* `python tools/loadtest.py URL [total] [concurrency]` — dependency-free load generator reporting req/s, p50/p99 and status mix.
* `python tools/refresh_cookies.py` — export cookies from a browser profile; `tools/browser_profile.sh` — open that profile; `tools/termux_setup.sh` — Android setup.

## 16. Project layout

```
main.py                 uvicorn launcher (workers, limits, uvloop/httptools)
app.py                  routes for pages, middleware wiring, lifespan (cookie watchdog, updater, doctor)
config/                 app name/version, server host/port
api/apis.py             public API (extract, status, result, instant, download)
api/admin.py            auth + admin API (stats, logs, tasks, anti-block, cookies)
engine/                 metadata_extractor (strategies, cache, singleflight) · downloader (streaming) · instant
advanced/               stealth · strategy_engine · proxy_manager · pot_provider · cookie_manager · updater · doctor
                        error_handler · retry_manager · url_security · header_manager · extractor_config
helpers/                logger · middleware · metrics · ratelimit · netinfo · auth · status_manager · redis_store
web/                    index, instant_save, download_video (Proper Save), admin, login, docs, ui.css, partials
tools/                  loadtest · refresh_cookies · browser_profile · termux_setup
tests/                  test_antiblock · test_scale
Dockerfile · start.sh · render.yaml · requirements.txt · requirements-prod.txt · .env.example
```
