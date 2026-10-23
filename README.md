# Downflow Backend

## Extraction engine

Extraction runs as a background task with live status:

* `POST /api/v1/extract/` — body `{"url": "...", "metadata_only": false, "format": "best"}`. Bad input (syntax, SSRF, format) is rejected immediately with `{"status":"error","error":{...}}` (400 etc.); otherwise **202** with `{"task_id","status":"queued","task":{...},"links":{...},"error":null}`.
  * `format`: `best` (best quality, may be separate video+audio), `best_single`, `worst`, `audio`, `video`, or a raw yt-dlp selector.
* `GET /api/v1/extract/status/{task_id}/` — `{task_id, status (queued|processing|completed|failed), stage, stage_label, progress, message, created_at, updated_at, completed_at, error, history}`. 404 for unknown/expired IDs. `progress` is `null` during extraction (yt-dlp gives no reliable percentage); stages report real engine steps: validating_url → initializing_extractor → extracting_metadata (→ retrying) → resolving_formats.
* `GET /api/v1/extract/result/{task_id}/` — `{task_id, status, data, error}`: 200 when completed, 202 while queued/processing ("not ready"), the recorded sanitised error with its HTTP status when failed, 404 if unknown.
* Result `data.download_url` is the direct media URL when the selection is a single stream; `null` (with `download_url_note`) for metadata-only requests, separate audio/video streams, or when the extractor exposes no usable URL. `download_url_info` carries format, headers, expiry hint and `verified: false` (URLs are never fetched or checked). Each `formats[]` entry has its own `download_url`.
* Task state is **in memory** (thread-safe, expires after `DOWNFLOW_TASK_TTL`, capped by `DOWNFLOW_MAX_TASKS`). With multiple workers/instances a status request can hit a process that doesn't know the task (404); use shared storage such as Redis behind `helpers/status_manager.py` for those deployments. Unfinished tasks are failed with `cancelled` on shutdown.
* `GET /download/` — web test page (`web/download_video.html`).
* Code: `engine/metadata_extractor.py` (framework independent), `advanced/` (config, cookies, headers, retries, errors, SSRF checks).

### Setup
```
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000     # or: python main.py
```
YouTube extraction needs a JavaScript runtime (Deno recommended, or Node) on PATH; `yt-dlp[default]` ships the solver scripts.
Keep yt-dlp current (`pip install -U "yt-dlp[default]"`) — extractor breakage is the most common failure.

### Environment variables (all optional, prefix `DOWNFLOW_`)
| Name | Default | Purpose |
|---|---|---|
| `SOCKET_TIMEOUT` / `REQUEST_TIMEOUT` | 20 / 60 | per-socket / whole-request seconds |
| `EXTRACTOR_RETRIES`, `DOWNLOAD_RETRIES`, `FRAGMENT_RETRIES` | 2 | yt-dlp internal retries |
| `RETRY_MAX_ATTEMPTS`, `RETRY_BASE_DELAY`, `RETRY_MAX_DELAY`, `RETRY_RATE_LIMIT_ATTEMPTS` | 3, 1.5, 20, 2 | bounded backoff for transient errors only |
| `MAX_CONCURRENT`, `QUEUE_WAIT`, `MAX_QUEUED` | 4, 30, 50 | parallel extractions; seconds a queued task waits for a slot; queue length before 503 |
| `TASK_TTL`, `MAX_TASKS` | 900, 500 | seconds finished tasks stay retrievable; tracked-task cap |
| `MAX_URL_LENGTH`, `MAX_PLAYLIST_ENTRIES`, `DEFAULT_FORMAT` | 2048, 50, best | limits/defaults |
| `PROXY` | – | http/https/socks proxy URL |
| `COOKIE_FILE` | – | Netscape cookies.txt you are authorised to use (copied per request; never logged/returned) |
| `COOKIES_FROM_BROWSER` | – | explicit `browser[:profile]` on the server host |
| `ROTATE_USER_AGENT`, `USER_AGENT_FILE`, `ACCEPT_LANGUAGE` | false, advanced/headers/user-agents.txt, en-US,en;q=0.9 | header options |
| `ALLOWED_PORTS`, `CHECK_REDIRECTS`, `MAX_REDIRECTS`, `ALLOW_PRIVATE_HOSTS` | 80,443 / true / 5 / false | SSRF guard (`ALLOW_PRIVATE_HOSTS` is for development only) |
| `JS_RUNTIMES`, `REMOTE_COMPONENTS`, `VERBOSE` | deno,node / – / false | yt-dlp JS runtimes, optional `ejs:github`, debug logging |

### Known limitations
* Media URLs are often signed, short-lived, IP- or header-bound; many platforms expose no standalone URL. Separate audio/video streams must be merged by the client.
* SSRF checks cover syntax, DNS results, the submitted URL's redirect chain, and returned media hosts. yt-dlp resolves DNS itself, so DNS rebinding is not fully excluded — also apply network egress filtering.
* User-agent rotation is off by default and is not a bypass for rate limits or access controls.
* Concurrency limits are per process; with multiple workers the limit multiplies.

## Cookies, proxy and anti-bot settings

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
