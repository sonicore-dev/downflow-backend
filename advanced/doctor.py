"""Startup self-check: finds the setup problems that silently break YouTube extraction.

The most common ones: no JavaScript runtime / challenge solver (YouTube then returns "only images are
available"), no PO-token provider, no TLS impersonation, no proxy while the IP is flagged.
Logged at startup and shown in ``GET /api/v1/admin/antiblock``.
"""

from __future__ import annotations

import importlib.metadata as md
import logging
import shutil
import subprocess
from typing import Any

from . import cookie_manager, pot_provider, proxy_manager
from .extractor_config import ExtractorConfig

__all__ = ["run_checks", "log_report"]

log = logging.getLogger("downflow.doctor")


def _node_major() -> int | None:
    exe = shutil.which("node")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out.lstrip("v").split(".")[0])
    except Exception:  # noqa: BLE001
        return None


def _check(name: str, ok: bool, detail: str, fix: str = "", severity: str = "warn") -> dict[str, Any]:
    return {"check": name, "ok": ok, "detail": detail, "fix": "" if ok else fix, "severity": "ok" if ok else severity}


def run_checks(config: ExtractorConfig) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        out.append(_check("yt-dlp", True, md.version("yt-dlp")))
    except md.PackageNotFoundError:
        out.append(_check("yt-dlp", False, "not installed", "pip install -U 'yt-dlp[default]'", "error"))

    runtimes = {r: shutil.which(r) for r in ("deno", "node", "bun")}
    found = {r: p for r, p in runtimes.items() if p}
    node = _node_major()
    usable = bool(found.get("deno") or found.get("bun") or (found.get("node") and (node or 0) >= 20))
    out.append(_check("JavaScript runtime", usable,
                      ", ".join(found) or "none found" + (f" (node {node} is too old, need >= 20)" if node else ""),
                      "Install Node.js 20+ (Termux: `pkg install nodejs`; Debian: nodejs 20+) or Deno. Without it YouTube "
                      "returns only images ('Requested format is not available').", "error"))

    try:
        ejs = md.version("yt-dlp-ejs")
    except md.PackageNotFoundError:
        ejs = None
    solver_ok = bool(ejs) or "ejs:github" in config.remote_components
    out.append(_check("challenge solver", solver_ok, f"yt-dlp-ejs {ejs}" if ejs else "missing",
                      "pip install -U yt-dlp-ejs   (or set DOWNFLOW_REMOTE_COMPONENTS=ejs:github)", "error"))

    try:
        from . import db
        st = db.status()
        try:
            db.one("SELECT 1")
            st = db.status()
            if st["persistent"]:
                out.append(_check("database", True, f"Turso · {st['where']}"))
            else:
                out.append(_check("database", False, "local SQLite file (erased on redeploy where the disk is ephemeral)",
                                  "Set DOWNFLOW_TURSO_URL and DOWNFLOW_TURSO_TOKEN so cookies/updates/memory survive redeploys.", "warn"))
        except db.DbError as exc:
            out.append(_check("database", False, str(exc), "Check DOWNFLOW_TURSO_URL / DOWNFLOW_TURSO_TOKEN.", "error"))
    except Exception as exc:  # noqa: BLE001
        out.append(_check("database", False, exc.__class__.__name__, "", "warn"))

    try:
        import curl_cffi  # noqa: F401
        out.append(_check("TLS impersonation", True, "curl_cffi " + md.version("curl-cffi")))
    except Exception:  # noqa: BLE001
        out.append(_check("TLS impersonation", False, "curl_cffi not importable",
                          "pip install -U curl_cffi (not available on every platform, e.g. some Termux builds)"))

    pot = pot_provider.status(config)
    out.append(_check("PO token provider", bool(pot["alive"]), pot["detail"],
                      "Run the bgutil provider server (the Docker image starts it) or set DOWNFLOW_POT_URL. "
                      "Without it, mweb/ios routes are skipped."))

    n = len(proxy_manager.pool(config))
    out.append(_check("proxy", n > 0, f"{n} configured" if n else "none (direct connection)",
                      "If the server answers HTTP 429 / 'confirm you're not a bot', this IP is flagged: set "
                      "DOWNFLOW_PROXY to a residential proxy (use {sid} for sticky sessions)."))
    st = cookie_manager.cookie_status(config)
    have = st.get("source") in ("file", "browser")
    out.append(_check("cookies", have, f"source={st.get('source')}",
                      "No cookies: YouTube's bot check usually blocks flagged IPs. Export your cookies.txt and copy it to "
                      f"{config.default_cookie_file} (or upload it in the admin Cookies tab).", "info"))
    return out


def log_report(config: ExtractorConfig) -> None:
    for c in run_checks(config):
        if c["ok"]:
            log.info("check  ok    %-20s %s", c["check"], c["detail"])
        else:
            log.warning("check  FAIL  %-20s %s -> %s", c["check"], c["detail"], c["fix"])
