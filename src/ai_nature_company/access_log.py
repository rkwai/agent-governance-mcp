"""Query-free structured HTTP access logging for the hosted MCP process.

Uvicorn's default access log writes the raw request target, which can retain
transient OAuth material such as ``authorization_id``.  This logger records only
the route path, method, status, and duration.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

ACCESS_LOGGER_NAME = "ai_nature_company.access"
_SENSITIVE_SCOPE_KEYS = frozenset(
    {
        "query_string",
        "headers",
        "authorization",
        "cookie",
        "cookies",
        "body",
    }
)


def access_logger() -> logging.Logger:
    """Return the dedicated access logger; it never uses Uvicorn's access logger."""

    return logging.getLogger(ACCESS_LOGGER_NAME)


def sanitize_access_path(path: str) -> str:
    """Return a loggable route path with query, fragment, and controls removed."""

    cleaned = path.split("?", 1)[0].split("#", 1)[0]
    cleaned = "".join(
        character for character in cleaned if 32 <= ord(character) < 127 and character not in "?#"
    )
    if not cleaned.startswith("/"):
        return "/"
    return cleaned


def format_access_line(
    *,
    method: str,
    path: str,
    status_code: int,
    duration_ms: int,
) -> str:
    """Format one access line from already-sanitized fields."""

    if any(character in path for character in "?#"):
        raise ValueError("access log path must not contain a query or fragment")
    if "\n" in method or "\r" in method or "\n" in path or "\r" in path:
        raise ValueError("access log fields must be single-line")
    return f"http_access method={method} path={path} status={status_code} duration_ms={duration_ms}"


def _scope_path(scope: Scope) -> str:
    raw_path = scope.get("path")
    if isinstance(raw_path, str) and raw_path.startswith("/"):
        return sanitize_access_path(raw_path)
    raw = scope.get("raw_path")
    if isinstance(raw, (bytes, bytearray)):
        return sanitize_access_path(raw.decode("latin-1", errors="replace"))
    return "/"


class QueryFreeAccessLogMiddleware:
    """ASGI middleware that logs method, path, status, and duration only."""

    def __init__(self, app: ASGIApp, *, logger: logging.Logger | None = None) -> None:
        self.app = app
        self.logger = logger or access_logger()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "UNKNOWN")
        path = _scope_path(scope)
        status_code = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = max(0, int((time.perf_counter() - started) * 1000))
            logged_method = sanitize_access_path(f"/{method}").lstrip("/") or "UNKNOWN"
            logged_path = sanitize_access_path(path)
            try:
                line = format_access_line(
                    method=logged_method,
                    path=logged_path,
                    status_code=status_code,
                    duration_ms=duration_ms,
                )
            except ValueError:
                logged_method = "UNKNOWN"
                logged_path = "/"
                line = format_access_line(
                    method=logged_method,
                    path=logged_path,
                    status_code=status_code,
                    duration_ms=duration_ms,
                )
            extra = {
                "http_method": logged_method,
                "http_path": logged_path,
                "http_status": status_code,
                "http_duration_ms": duration_ms,
            }
            for key in _SENSITIVE_SCOPE_KEYS:
                extra.pop(key, None)
            self.logger.info(line, extra=extra)


def wrap_with_query_free_access_log(
    app: ASGIApp,
    *,
    logger: logging.Logger | None = None,
) -> QueryFreeAccessLogMiddleware:
    """Wrap an ASGI app with the query-free access logger."""

    return QueryFreeAccessLogMiddleware(app, logger=logger)


# Starlette middleware protocol compatibility for type checkers that expect call().
MiddlewareApp = Callable[[Scope, Receive, Send], Awaitable[None]]
