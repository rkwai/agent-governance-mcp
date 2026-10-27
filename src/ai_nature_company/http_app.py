"""Authenticated production ASGI boundary for Streamable HTTP MCP.

This module composes the official MCP SDK transport with two deliberately
separate authentication dependencies:

* ``TokenVerifier`` verifies the bearer token and produces SDK ``AccessToken``
  context.
* ``TokenAuthenticator`` resolves that verified context to one immutable
  application ``VerifiedPrincipal``.

JWT validation and principal persistence belong behind those interfaces.  The
HTTP adapter never derives authority from request payload fields.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit

from mcp.server import MCPServer
from mcp.server.auth.handlers.metadata import ProtectedResourceMetadataHandler
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url, cors_middleware
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import ProtectedResourceMetadata
from pydantic import AnyHttpUrl, TypeAdapter
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from .mcp_resource import (
    bind_request_mcp_resource_url,
    is_mcp_resource_path_slug,
    reset_request_mcp_resource_url,
    resource_url_for_path,
)
from .server import (
    MAX_PAYLOAD_BYTES,
    SDKAccessTokenResolver,
    ServerLifespan,
    SubstrateService,
    TokenAuthenticator,
    ToolCallAuditSink,
    build_server,
)

DEFAULT_REQUIRED_SCOPE = "openid"
DEFAULT_MAX_HTTP_REQUEST_BYTES = MAX_PAYLOAD_BYTES * 4
_HTTP_URL = TypeAdapter(AnyHttpUrl)

type ServerConfigurator = Callable[[MCPServer[Any]], None]


def _as_http_url(value: str) -> AnyHttpUrl:
    return _HTTP_URL.validate_python(value)


def _public_https_url(value: object, field_name: str) -> SplitResult:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a canonical URL")
    parsed = urlsplit(value)
    if parsed.scheme != "https":
        raise ValueError(f"{field_name} must use HTTPS")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must contain a public host without user information")
    if parsed.query:
        raise ValueError(f"{field_name} must not contain a query")
    if parsed.fragment:
        raise ValueError(f"{field_name} must not contain a fragment")
    return parsed


def _canonical_mcp_path(parsed: SplitResult) -> str:
    path = parsed.path
    if not path or path == "/":
        raise ValueError("resource_server_url must contain a non-root MCP path")
    if not path.startswith("/") or path.endswith("/") or "//" in path:
        raise ValueError("resource_server_url must contain a canonical MCP path")
    if any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("resource_server_url must contain a canonical MCP path")
    return path


def _canonical_string_tuple(
    values: object,
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError(f"{field_name} must be an iterable of strings, not a string")
    try:
        materialized = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError(f"{field_name} must be an iterable of strings") from error
    if not allow_empty and not materialized:
        raise ValueError(f"{field_name} must not be empty")
    for value in materialized:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f"{field_name} must contain canonical non-empty strings")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError(f"{field_name} must not contain control characters")
    if len(set(materialized)) != len(materialized):
        raise ValueError(f"{field_name} must not contain duplicates")
    return materialized


@dataclass(frozen=True, slots=True)
class HTTPAppSettings:
    """Validated public-resource settings for one hosted MCP endpoint.

    TLS is normally terminated by the deployment ingress.  Requiring HTTPS in
    the advertised URLs prevents OAuth metadata and challenges from directing
    clients to a plaintext public resource or issuer.
    """

    issuer_url: str
    resource_server_url: str
    required_scopes: tuple[str, ...] = (DEFAULT_REQUIRED_SCOPE,)
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    stateless_http: bool = True
    json_response: bool = True
    max_request_body_size: int = DEFAULT_MAX_HTTP_REQUEST_BYTES
    service_documentation_url: str | None = None

    def __post_init__(self) -> None:
        _public_https_url(self.issuer_url, "issuer_url")
        resource = _public_https_url(self.resource_server_url, "resource_server_url")
        _canonical_mcp_path(resource)
        if self.service_documentation_url is not None:
            _public_https_url(self.service_documentation_url, "service_documentation_url")

        scopes = _canonical_string_tuple(
            self.required_scopes,
            "required_scopes",
            allow_empty=False,
        )
        hosts = _canonical_string_tuple(
            self.allowed_hosts,
            "allowed_hosts",
            allow_empty=True,
        )
        origins = _canonical_string_tuple(
            self.allowed_origins,
            "allowed_origins",
            allow_empty=True,
        )
        if not isinstance(self.stateless_http, bool):
            raise TypeError("stateless_http must be a boolean")
        if not isinstance(self.json_response, bool):
            raise TypeError("json_response must be a boolean")
        if not isinstance(self.max_request_body_size, int) or isinstance(
            self.max_request_body_size,
            bool,
        ):
            raise TypeError("max_request_body_size must be an integer")
        if self.max_request_body_size <= 0:
            raise ValueError("max_request_body_size must be positive")

        object.__setattr__(self, "required_scopes", scopes)
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "allowed_origins", origins)

    @property
    def streamable_http_path(self) -> str:
        return _canonical_mcp_path(urlsplit(self.resource_server_url))

    @property
    def effective_allowed_hosts(self) -> tuple[str, ...]:
        if self.allowed_hosts:
            return self.allowed_hosts
        # ``netloc`` preserves an explicitly configured non-default port, which
        # must then match the ingress Host header exactly.
        return (urlsplit(self.resource_server_url).netloc,)

    def resource_url_for_request_path(self, path: str) -> str | None:
        """Return the public resource URL for a local MCP path, or None."""

        return resource_url_for_path(self.resource_server_url, path)


def build_http_app(
    service: SubstrateService,
    *,
    token_verifier: TokenVerifier,
    authenticate_token: TokenAuthenticator,
    settings: HTTPAppSettings,
    lifespan: ServerLifespan | None = None,
    call_audit_sink: ToolCallAuditSink | None = None,
    configure_server: ServerConfigurator | None = None,
) -> Starlette:
    """Build a fail-closed authenticated Streamable HTTP ASGI application.

    ``authenticate_token`` receives only an access token already accepted by
    ``token_verifier``.  A production Supabase adapter can therefore verify JWT
    signature/claims in the first dependency and load the live principal, grants,
    cell binding, and run in the second without changing this transport module.
    """

    if token_verifier is None or not callable(getattr(token_verifier, "verify_token", None)):
        raise TypeError("token_verifier is required and must implement verify_token")
    if authenticate_token is None or not callable(authenticate_token):
        raise TypeError("authenticate_token is required and must be callable")
    if not isinstance(settings, HTTPAppSettings):
        raise TypeError("settings must be HTTPAppSettings")
    if configure_server is not None and not callable(configure_server):
        raise TypeError("configure_server must be callable")

    auth = AuthSettings(
        issuer_url=settings.issuer_url,
        resource_server_url=settings.resource_server_url,
        service_documentation_url=settings.service_documentation_url,
        required_scopes=list(settings.required_scopes),
    )
    server = build_server(
        service,
        SDKAccessTokenResolver(authenticate_token),
        lifespan=lifespan,
        auth=auth,
        token_verifier=token_verifier,
        call_audit_sink=call_audit_sink,
    )
    if configure_server is not None:
        configure_server(server)
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.effective_allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )
    app = server.streamable_http_app(
        streamable_http_path=settings.streamable_http_path,
        json_response=settings.json_response,
        stateless_http=settings.stateless_http,
        max_request_body_size=settings.max_request_body_size,
        transport_security=transport_security,
        host=urlsplit(settings.resource_server_url).hostname or "invalid",
    )
    _attach_path_alias_routes(
        app,
        settings=settings,
        required_scopes=list(settings.required_scopes),
    )
    # Keep non-secret composition details available to deployment diagnostics
    # without creating a second authority path.
    app.state.mcp_server = server
    app.state.http_settings = settings
    return app


def _streamable_http_inner_app(app: Starlette, path: str) -> Any:
    for route in app.router.routes:
        if getattr(route, "path", None) != path:
            continue
        endpoint = getattr(route, "endpoint", None)
        if isinstance(endpoint, RequireAuthMiddleware):
            return endpoint.app
        return endpoint
    raise RuntimeError("streamable HTTP route is missing")


class _PathAliasMCPEndpoint:
    """Serve the same Streamable HTTP app at ``<primary>/<slug>``.

    OAuth metadata and challenges for that path advertise the path URL, not the
    process primary ``/mcp``.  Invalid slugs fail closed with 404.
    """

    def __init__(
        self,
        inner: Any,
        settings: HTTPAppSettings,
        required_scopes: list[str],
    ) -> None:
        self._inner = inner
        self._settings = settings
        self._required_scopes = required_scopes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path")
        resource = (
            self._settings.resource_url_for_request_path(path) if isinstance(path, str) else None
        )
        if resource is None:
            await Response(status_code=404)(scope, receive, send)
            return
        token = bind_request_mcp_resource_url(resource)
        try:
            await RequireAuthMiddleware(
                self._inner,
                self._required_scopes,
                build_resource_metadata_url(_as_http_url(resource)),
            )(scope, receive, send)
        finally:
            reset_request_mcp_resource_url(token)

    async def handle_metadata(self, request: Request) -> Response:
        slug = request.path_params.get("slug")
        if not is_mcp_resource_path_slug(slug):
            return Response(status_code=404)
        resource = self._settings.resource_url_for_request_path(
            f"{self._settings.streamable_http_path}/{slug}"
        )
        if resource is None:
            return Response(status_code=404)
        metadata = ProtectedResourceMetadata(
            resource=_as_http_url(resource),
            authorization_servers=[_as_http_url(self._settings.issuer_url)],
            scopes_supported=list(self._settings.required_scopes),
        )
        return await ProtectedResourceMetadataHandler(metadata).handle(request)


def _attach_path_alias_routes(
    app: Starlette,
    *,
    settings: HTTPAppSettings,
    required_scopes: list[str],
) -> None:
    """Mount conservative ``<primary>/<slug>`` aliases after the primary ``/mcp``."""

    inner = _streamable_http_inner_app(app, settings.streamable_http_path)
    endpoint = _PathAliasMCPEndpoint(inner, settings, required_scopes)
    primary = settings.streamable_http_path
    app.router.routes.extend(
        (
            Route(f"{primary}/{{slug}}", endpoint=endpoint),
            Route(
                f"/.well-known/oauth-protected-resource{primary}/{{slug}}",
                endpoint=cors_middleware(endpoint.handle_metadata, ["GET", "OPTIONS"]),
                methods=["GET", "OPTIONS"],
            ),
        )
    )


__all__ = [
    "DEFAULT_MAX_HTTP_REQUEST_BYTES",
    "DEFAULT_REQUIRED_SCOPE",
    "HTTPAppSettings",
    "ServerConfigurator",
    "build_http_app",
]
