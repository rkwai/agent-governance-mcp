"""Canonical public MCP resource URLs and conservative path aliases.

A hosted process has one primary protected resource, typically
``https://<host>/mcp``.  The same process may also present
``https://<host><primary-path>/<slug>`` so several Grok connectors can keep
distinct OAuth resource URLs without extra Railway services.  Path is not a
host: allowlists stay host-based, and identity still exact-matches the
request's full resource URL to the client's primary row or an active alias.
"""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from urllib.parse import urlsplit, urlunsplit

_request_mcp_resource_url: ContextVar[str | None] = ContextVar(
    "anc_request_mcp_resource_url",
    default=None,
)

MAX_MCP_RESOURCE_PATH_SLUG_LENGTH = 64
MCP_RESOURCE_PATH_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Additive superset of the 202608310007 alias CHECK: primary /mcp URLs remain
# valid, and one conservative slug may follow that path.
MCP_PATH_ALIAS_RESOURCE_URL_PATTERN = r"^https://[^?#]+/mcp(?:/[a-z0-9]+(?:-[a-z0-9]+)*)?$"


def request_mcp_resource_url() -> str | None:
    """Return the public MCP resource URL for the current HTTP request, if any."""

    return _request_mcp_resource_url.get()


def bind_request_mcp_resource_url(resource_url: str) -> Token[str | None]:
    """Bind the request resource URL until the caller resets the token."""

    return _request_mcp_resource_url.set(resource_url)


def reset_request_mcp_resource_url(token: Token[str | None]) -> None:
    _request_mcp_resource_url.reset(token)


def is_mcp_resource_path_slug(value: object) -> bool:
    """Return True for a conservative MCP path-alias identifier."""

    if not isinstance(value, str) or not value:
        return False
    if len(value) > MAX_MCP_RESOURCE_PATH_SLUG_LENGTH:
        return False
    return MCP_RESOURCE_PATH_SLUG_PATTERN.fullmatch(value) is not None


def mcp_path_alias(primary_path: str, slug: str) -> str | None:
    """Return ``<primary-path>/<slug>`` when both parts are canonical."""

    if (
        not isinstance(primary_path, str)
        or not primary_path.startswith("/")
        or primary_path.endswith("/")
        or "//" in primary_path
        or any(part in {".", ".."} for part in primary_path.split("/"))
    ):
        return None
    if not is_mcp_resource_path_slug(slug):
        return None
    return f"{primary_path}/{slug}"


def is_local_mcp_resource_url(primary_resource_url: str, candidate: str) -> bool:
    """Accept the process primary URL or a same-origin conservative path alias."""

    if candidate == primary_resource_url:
        return True
    try:
        primary = urlsplit(primary_resource_url)
        parsed = urlsplit(candidate)
    except ValueError:
        return False
    if (parsed.scheme, parsed.netloc) != (primary.scheme, primary.netloc):
        return False
    if parsed.query or parsed.fragment:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    prefix = f"{primary.path}/"
    if not parsed.path.startswith(prefix):
        return False
    return is_mcp_resource_path_slug(parsed.path[len(prefix) :])


def resource_url_for_path(primary_resource_url: str, request_path: str) -> str | None:
    """Map a local request path to the public resource URL, or None."""

    try:
        primary = urlsplit(primary_resource_url)
    except ValueError:
        return None
    if request_path == primary.path:
        return primary_resource_url
    alias_path = None
    prefix = f"{primary.path}/"
    if request_path.startswith(prefix):
        alias_path = mcp_path_alias(primary.path, request_path[len(prefix) :])
    if alias_path is None:
        return None
    return urlunsplit((primary.scheme, primary.netloc, alias_path, "", ""))


__all__ = [
    "MAX_MCP_RESOURCE_PATH_SLUG_LENGTH",
    "MCP_PATH_ALIAS_RESOURCE_URL_PATTERN",
    "MCP_RESOURCE_PATH_SLUG_PATTERN",
    "bind_request_mcp_resource_url",
    "is_local_mcp_resource_url",
    "is_mcp_resource_path_slug",
    "mcp_path_alias",
    "request_mcp_resource_url",
    "reset_request_mcp_resource_url",
    "resource_url_for_path",
]
