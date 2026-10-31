# Client IP resolution that cannot be spoofed with a forged X-Forwarded-For.
#
# ``DOWNFLOW_CLIENT_IP_HEADER`` (for example ``CF-Connecting-IP``) wins when present. Otherwise the address is
# taken ``DOWNFLOW_PROXY_HOPS`` entries from the RIGHT of X-Forwarded-For (each trusted proxy appends the
# address it saw), so anything a client prepends is ignored. With 0 hops the TCP peer is used.
# IPv6 addresses are collapsed to their /64 so one client cannot dodge limits by rotating addresses.

from __future__ import annotations

import ipaddress
from typing import Iterable, Optional

from advanced.extractor_config import get_config

__all__ = ["client_ip", "limit_key"]


def client_ip(headers: Iterable[tuple[bytes, bytes]], peer: Optional[str]) -> str:
    cfg = get_config()
    want = (cfg.client_ip_header or "").lower().encode()
    fwd = None
    for k, v in headers:
        if want and k == want:
            return v.decode("latin-1").strip()[:64] or (peer or "?")
        if k == b"x-forwarded-for":
            fwd = v.decode("latin-1")
    if cfg.proxy_hops > 0 and fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[max(0, len(parts) - cfg.proxy_hops)][:64]
    return peer or "?"


def limit_key(ip: str) -> str:
    # Key used for rate limiting: IPv6 -> /64 network, IPv4 unchanged.
    if ":" in ip:
        try:
            return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address)
        except ValueError:
            pass
    return ip
