"""Ingress security for the hub HTTP API.

Posture: NO unauthenticated endpoints, including /health. Rate limiting
runs before auth so key-guessing gets throttled too. TLS/network exposure
is handled outside the app (Tailscale mesh; Caddy if anything must be
public) — see README security notes.
"""

from __future__ import annotations

import secrets
import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from jarvis.core.logging import get_logger

log = get_logger(__name__)

API_KEY_HEADER = "x-api-key"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window per-client-IP limiter. In-memory is fine: single
    process, and a restart resetting counters is harmless."""

    def __init__(self, app, limit_per_min: int = 120):
        super().__init__(app)
        self.limit = limit_per_min
        self.window_s = 60.0
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        client = request.client.host if request.client else "unknown"
        now = time.monotonic()
        q = self.hits[client]
        while q and now - q[0] > self.window_s:
            q.popleft()
        if len(q) >= self.limit:
            log.warning("rate_limited", client=client)
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
        q.append(now)
        return await call_next(request)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, api_key: str):
        super().__init__(app)
        if not api_key:
            raise ValueError(
                "JARVIS_API_KEY is not set — refusing to start an unauthenticated "
                "hub. Run `jarvis keygen`."
            )
        self.api_key = api_key

    async def dispatch(self, request: Request, call_next):
        supplied = request.headers.get(API_KEY_HEADER, "")
        if not secrets.compare_digest(supplied, self.api_key):
            client = request.client.host if request.client else "unknown"
            log.warning("auth_rejected", client=client, path=request.url.path)
            return JSONResponse({"detail": "invalid or missing API key"}, status_code=401)
        return await call_next(request)


class KillSwitchMiddleware(BaseHTTPMiddleware):
    """When paused, everything except health/control returns 503 so the
    operator can always inspect state and resume."""

    EXEMPT_PREFIXES = ("/health", "/control")

    def __init__(self, app, killswitch):
        super().__init__(app)
        self.killswitch = killswitch

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith(self.EXEMPT_PREFIXES) and self.killswitch.is_paused():
            return JSONResponse(
                {"detail": "JARVIS is paused (kill switch engaged)"}, status_code=503
            )
        return await call_next(request)
