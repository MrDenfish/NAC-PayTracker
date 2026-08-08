"""Real client IP resolution behind the Cloudflare → Caddy proxy chain.

The socket peer is always the Caddy container (and Caddy's peer is a
Cloudflare edge), so the true client address only exists in headers.
``CF-Connecting-IP`` is set by Cloudflare and can't be spoofed through
the edge; ``X-Forwarded-For`` is the general fallback; the socket host
covers direct/dev access.
"""

from __future__ import annotations

from starlette.requests import Request


def client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # First hop is the original client.
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
