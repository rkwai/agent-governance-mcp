"""Production process composition for the authenticated HTTP MCP substrate.

The hosted process has one shared PostgreSQL pool, one deterministic substrate
service, one Supabase JWT verifier, and one server-owned subject resolver.  It
derives no authority from request arguments and never needs a Supabase service-role
key at runtime.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from psycopg_pool import AsyncConnectionPool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .access_log import QueryFreeAccessLogMiddleware
from .attribution import ToolCallAuditSink
from .database_url import require_tls_database_url
from .http_app import HTTPAppSettings, build_http_app
from .migration_readiness import MigrationReadinessReport, assess_database_migrations
from .postgres import PostgresRepository
from .postgres_identity import (
    PostgresSubjectIdentityRepository,
    fetch_expiring_agent_runs,
    format_expiring_agent_run_warning,
)
from .server import SubstrateService, TokenAuthenticator
from .service import create_service
from .store import Repository
from .supabase_auth import (
    SubjectIdentityRepository,
    SupabaseIdentityAuthenticator,
    SupabaseJWTSettings,
    SupabaseJWTVerifier,
)

DEFAULT_DATABASE_POOL_MIN_SIZE = 1
DEFAULT_DATABASE_POOL_MAX_SIZE = 10
DEFAULT_DATABASE_STARTUP_TIMEOUT_SECONDS = 30
DEFAULT_DATABASE_READINESS_TIMEOUT_SECONDS = 5
AGENT_RUN_EXPIRY_WARNING_INTERVAL_SECONDS = 15 * 60
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
DEFAULT_WORKERS = 1
DEFAULT_RUNTIME_DATABASE_ROLE = "ai_nature_company_runtime"

_GOVERNED_TABLE_PRIVILEGES: Mapping[str, frozenset[str]] = {
    "anc_stream_heads": frozenset({"select", "insert", "update"}),
    "anc_domain_events": frozenset({"select", "insert"}),
    "anc_audit_events": frozenset({"select", "insert"}),
    "anc_projections": frozenset({"select", "insert", "update"}),
    "anc_idempotency_records": frozenset({"select", "insert", "update"}),
    "anc_budgets": frozenset({"select", "insert", "update"}),
    "anc_budget_reservations": frozenset({"select", "insert", "update"}),
    "anc_work_leases": frozenset({"select", "insert", "update"}),
    "anc_oauth_clients": frozenset(),
    "anc_oauth_client_resource_aliases": frozenset(),
    "anc_auth_subjects": frozenset(),
    "anc_agent_instances": frozenset(),
    "anc_agent_runs": frozenset(),
    "anc_mcp_tool_calls": frozenset({"select", "insert"}),
}
_GOVERNED_SEQUENCES = (
    "anc_domain_events_position_seq",
    "anc_audit_events_position_seq",
    "anc_mcp_tool_calls_position_seq",
)
LOGGER = logging.getLogger(__name__)


def _canonical_environment_value(
    environ: Mapping[str, str],
    name: str,
    *,
    required: bool,
) -> str | None:
    value = environ.get(name)
    if value is None or not value:
        if required:
            raise RuntimeError(f"{name} is required")
        return None
    if value != value.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError(f"{name} must be a canonical environment value")
    return value


def _csv_environment(
    environ: Mapping[str, str],
    name: str,
    *,
    required: bool,
) -> tuple[str, ...]:
    value = _canonical_environment_value(environ, name, required=required)
    if value is None:
        return ()
    parts = tuple(part.strip() for part in value.split(","))
    if not parts or any(not part for part in parts):
        message = "must contain at least one value" if required else "contains an empty value"
        raise ValueError(f"{name} {message}")
    if len(set(parts)) != len(parts):
        raise ValueError(f"{name} must not contain duplicates")
    return parts


def _bounded_integer_environment(
    environ: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = _canonical_environment_value(environ, name, required=False)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be from {minimum} through {maximum}")
    return value


def _require_https_url(value: str, field_name: str, *, origin: bool = False) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field_name} must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field_name} must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field_name} must not contain a query or fragment")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{field_name} must contain a valid optional port") from error
    del port
    if origin and parsed.path:
        raise ValueError(f"{field_name} must be an HTTPS origin without a path")
    return value


def _validate_allowed_host(value: str) -> str:
    if "*" in value:
        raise ValueError("allowed hosts must not contain wildcards")
    if "://" in value or "/" in value or "@" in value:
        raise ValueError("allowed hosts must contain only a host and optional port")
    parsed = urlsplit(f"//{value}")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("allowed hosts must contain a valid optional port") from error
    del port
    if not parsed.hostname:
        raise ValueError("allowed hosts must contain a host")
    return value


@dataclass(frozen=True, slots=True)
class HostedSettings:
    """Validated, fail-closed settings for one production MCP resource."""

    database_url: str = field(repr=False)
    supabase_url: str
    supabase_publishable_key: str = field(repr=False)
    supabase_issuer: str
    supabase_audience: str
    public_mcp_url: str
    allowed_client_ids: frozenset[str]
    supabase_jwks_url: str | None = None
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    service_documentation_url: str | None = None
    pool_min_size: int = DEFAULT_DATABASE_POOL_MIN_SIZE
    pool_max_size: int = DEFAULT_DATABASE_POOL_MAX_SIZE
    database_startup_timeout_seconds: int = DEFAULT_DATABASE_STARTUP_TIMEOUT_SECONDS
    runtime_database_role: str = DEFAULT_RUNTIME_DATABASE_ROLE

    def __post_init__(self) -> None:
        if not isinstance(self.database_url, str) or not self.database_url:
            raise ValueError("database_url must not be empty")
        if self.database_url != self.database_url.strip():
            raise ValueError("database_url must be canonical")
        require_tls_database_url(self.database_url)
        _require_https_url(self.supabase_url, "supabase_url", origin=True)
        _require_https_url(self.supabase_issuer, "supabase_issuer")
        if self.supabase_issuer != f"{self.supabase_url}/auth/v1":
            raise ValueError("supabase_issuer must equal supabase_url plus /auth/v1")
        if (
            not isinstance(self.supabase_publishable_key, str)
            or not self.supabase_publishable_key
            or self.supabase_publishable_key != self.supabase_publishable_key.strip()
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in self.supabase_publishable_key
            )
        ):
            raise ValueError("supabase_publishable_key must be a canonical non-empty string")
        if self.supabase_jwks_url is not None:
            _require_https_url(self.supabase_jwks_url, "supabase_jwks_url")
        if self.service_documentation_url is not None:
            _require_https_url(
                self.service_documentation_url,
                "service_documentation_url",
            )
        try:
            client_ids = frozenset(self.allowed_client_ids)
        except TypeError as error:
            raise TypeError("allowed_client_ids must be an iterable of strings") from error
        if not client_ids:
            raise ValueError("allowed_client_ids must contain at least one value")
        if not all(isinstance(client_id, str) and client_id for client_id in client_ids):
            raise ValueError("allowed_client_ids must contain non-empty strings")
        hosts = tuple(_validate_allowed_host(host) for host in self.allowed_hosts)
        origins = tuple(
            _require_https_url(origin, "allowed origin", origin=True)
            for origin in self.allowed_origins
        )
        if len(set(hosts)) != len(hosts):
            raise ValueError("allowed_hosts must not contain duplicates")
        if len(set(origins)) != len(origins):
            raise ValueError("allowed_origins must not contain duplicates")
        if (
            isinstance(self.pool_min_size, bool)
            or not isinstance(self.pool_min_size, int)
            or self.pool_min_size < 1
        ):
            raise ValueError("pool_min_size must be a positive integer")
        if (
            isinstance(self.pool_max_size, bool)
            or not isinstance(self.pool_max_size, int)
            or self.pool_max_size < 1
        ):
            raise ValueError("pool_max_size must be a positive integer")
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("pool_min_size cannot exceed pool_max_size")
        if (
            isinstance(self.database_startup_timeout_seconds, bool)
            or not isinstance(self.database_startup_timeout_seconds, int)
            or not 1 <= self.database_startup_timeout_seconds <= 300
        ):
            raise ValueError("database_startup_timeout_seconds must be from 1 through 300")
        if (
            not isinstance(self.runtime_database_role, str)
            or not self.runtime_database_role
            or self.runtime_database_role != self.runtime_database_role.strip()
            or not self.runtime_database_role.isidentifier()
        ):
            raise ValueError("runtime_database_role must be a canonical SQL identifier")
        object.__setattr__(self, "allowed_client_ids", client_ids)
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "allowed_origins", origins)

        # Reuse the transport and JWT contracts here so configuration fails at
        # process startup, before a listener can advertise unusable metadata.
        validated_dependencies = (self.http_settings, self.jwt_settings)
        del validated_dependencies

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> HostedSettings:
        """Load production settings without ever including secret values in errors."""

        database_url = _canonical_environment_value(
            environ,
            "DATABASE_URL",
            required=True,
        )
        supabase_url = _canonical_environment_value(
            environ,
            "ANC_SUPABASE_URL",
            required=True,
        )
        publishable_key = _canonical_environment_value(
            environ,
            "ANC_SUPABASE_PUBLISHABLE_KEY",
            required=True,
        )
        issuer = _canonical_environment_value(
            environ,
            "ANC_SUPABASE_ISSUER",
            required=True,
        )
        audience = _canonical_environment_value(
            environ,
            "ANC_SUPABASE_AUDIENCE",
            required=True,
        )
        public_mcp_url = _canonical_environment_value(
            environ,
            "ANC_PUBLIC_MCP_URL",
            required=True,
        )
        assert database_url is not None
        assert supabase_url is not None
        assert publishable_key is not None
        assert issuer is not None
        assert audience is not None
        assert public_mcp_url is not None
        return cls(
            database_url=database_url,
            supabase_url=supabase_url,
            supabase_publishable_key=publishable_key,
            supabase_issuer=issuer,
            supabase_audience=audience,
            public_mcp_url=public_mcp_url,
            allowed_client_ids=frozenset(
                _csv_environment(
                    environ,
                    "ANC_SUPABASE_CLIENT_IDS",
                    required=True,
                )
            ),
            supabase_jwks_url=_canonical_environment_value(
                environ,
                "ANC_SUPABASE_JWKS_URL",
                required=False,
            ),
            allowed_hosts=_csv_environment(
                environ,
                "ANC_ALLOWED_HOSTS",
                required=False,
            ),
            allowed_origins=_csv_environment(
                environ,
                "ANC_ALLOWED_ORIGINS",
                required=False,
            ),
            service_documentation_url=_canonical_environment_value(
                environ,
                "ANC_SERVICE_DOCUMENTATION_URL",
                required=False,
            ),
            pool_min_size=_bounded_integer_environment(
                environ,
                "ANC_DATABASE_POOL_MIN_SIZE",
                default=DEFAULT_DATABASE_POOL_MIN_SIZE,
                minimum=1,
                maximum=100,
            ),
            pool_max_size=_bounded_integer_environment(
                environ,
                "ANC_DATABASE_POOL_MAX_SIZE",
                default=DEFAULT_DATABASE_POOL_MAX_SIZE,
                minimum=1,
                maximum=100,
            ),
            database_startup_timeout_seconds=_bounded_integer_environment(
                environ,
                "ANC_DATABASE_STARTUP_TIMEOUT_SECONDS",
                default=DEFAULT_DATABASE_STARTUP_TIMEOUT_SECONDS,
                minimum=1,
                maximum=300,
            ),
            runtime_database_role=(
                _canonical_environment_value(
                    environ,
                    "ANC_RUNTIME_DATABASE_ROLE",
                    required=False,
                )
                or DEFAULT_RUNTIME_DATABASE_ROLE
            ),
        )

    @property
    def jwt_settings(self) -> SupabaseJWTSettings:
        return SupabaseJWTSettings(
            issuer=self.supabase_issuer,
            audience=self.supabase_audience,
            resource_server_url=self.public_mcp_url,
            allowed_client_ids=self.allowed_client_ids,
            jwks_url=self.supabase_jwks_url,
        )

    @property
    def http_settings(self) -> HTTPAppSettings:
        return HTTPAppSettings(
            issuer_url=self.supabase_issuer,
            resource_server_url=self.public_mcp_url,
            required_scopes=("openid",),
            allowed_hosts=self.allowed_hosts,
            allowed_origins=self.allowed_origins,
            stateless_http=True,
            json_response=True,
            service_documentation_url=self.service_documentation_url,
        )

    @property
    def supabase_consent_ui_url(self) -> str:
        public = urlsplit(self.public_mcp_url)
        return urlunsplit((public.scheme, public.netloc, "/oauth/consent", "", ""))


type SharedPool = AsyncConnectionPool[Any]
type RepositoryFactory = Callable[[SharedPool], Repository]
type IdentityRepositoryFactory = Callable[[SharedPool], SubjectIdentityRepository]
type AuditSinkFactory = Callable[[SharedPool], ToolCallAuditSink]
type ConsentRegistrar = Callable[[MCPServer[Any], HostedSettings], None]


@dataclass(frozen=True, slots=True)
class HostedRuntime:
    """Non-secret process dependencies retained for lifecycle diagnostics."""

    pool: SharedPool
    repository: Repository
    identity_repository: SubjectIdentityRepository
    audit_sink: ToolCallAuditSink
    service: SubstrateService
    token_verifier: TokenVerifier
    authenticate_token: TokenAuthenticator


@dataclass(slots=True)
class _Readiness:
    ready: bool = False
    last_expiry_warning_at: datetime | None = None


def _default_repository(pool: SharedPool) -> Repository:
    return PostgresRepository(pool=pool)


def _default_identity_repository(pool: SharedPool) -> SubjectIdentityRepository:
    return PostgresSubjectIdentityRepository(pool=pool)


def _default_audit_sink(pool: SharedPool) -> ToolCallAuditSink:
    # Imported lazily to keep the persistence adapter replaceable in composition
    # tests and to avoid module initialization doing any database work.
    from .postgres_attribution import PostgresToolCallAuditSink

    return PostgresToolCallAuditSink(pool=pool, transport="streamable_http")


def _default_consent_registrar(server: MCPServer[Any], settings: HostedSettings) -> None:
    from .consent import ConsentSettings, register_consent_routes

    register_consent_routes(
        server,
        ConsentSettings(
            supabase_url=settings.supabase_url,
            supabase_publishable_key=settings.supabase_publishable_key,
        ),
    )


async def _database_preflight(
    pool: SharedPool,
    *,
    runtime_role: str = DEFAULT_RUNTIME_DATABASE_ROLE,
) -> None:
    """Prove the runtime role and complete migration contract before readiness."""

    async with pool.connection() as connection:
        role_cursor = await connection.execute(
            """
            SELECT current_user = %s AS runtime_role_is_exact,
                   NOT (
                       role.rolsuper
                       OR role.rolbypassrls
                       OR role.rolcreatedb
                       OR role.rolcreaterole
                       OR role.rolinherit
                       OR role.rolreplication
                   ) AS runtime_role_is_safe,
                   has_schema_privilege(current_user, 'public', 'USAGE')
                       AS public_schema_usage,
                   NOT has_schema_privilege(current_user, 'public', 'CREATE')
                       AS public_schema_no_create,
                   NOT EXISTS (
                       SELECT 1
                       FROM pg_catalog.pg_auth_members AS membership
                       WHERE membership.member = role.oid
                   ) AS has_no_role_memberships
            FROM pg_catalog.pg_roles AS role
            WHERE role.rolname = current_user
            """,
            (runtime_role,),
        )
        role = await role_cursor.fetchone()
        if role is None or any(flag is not True for flag in role):
            raise RuntimeError("database runtime role does not satisfy least privilege")

        resolver_cursor = await connection.execute(
            """
            WITH resolver AS (
                SELECT to_regprocedure(
                    'public.anc_resolve_agent_identity(text,text,text,text,text)'
                ) AS resolver_oid
            )
            SELECT resolver_oid IS NOT NULL AS resolver_exists,
                   COALESCE(
                       has_function_privilege(current_user, resolver_oid, 'EXECUTE'),
                       false
                   ) AS resolver_execute
            FROM resolver
            """
        )
        resolver = await resolver_cursor.fetchone()
        if resolver is None or resolver[0] is not True or resolver[1] is not True:
            raise RuntimeError("database identity resolver is missing or not executable")

        catalog_function_cursor = await connection.execute(
            """
            WITH compatibility_function AS (
                SELECT to_regprocedure(
                    'public.anc_auth_catalog_compatible()'
                ) AS function_oid
            )
            SELECT function_oid IS NOT NULL AS compatibility_function_exists,
                   COALESCE(
                       has_function_privilege(current_user, function_oid, 'EXECUTE'),
                       false
                   ) AS compatibility_function_execute
            FROM compatibility_function
            """
        )
        catalog_function = await catalog_function_cursor.fetchone()
        if (
            catalog_function is None
            or catalog_function[0] is not True
            or catalog_function[1] is not True
        ):
            raise RuntimeError(
                "database auth-catalog compatibility check is missing or not executable"
            )
        catalog_cursor = await connection.execute("SELECT public.anc_auth_catalog_compatible()")
        catalog = await catalog_cursor.fetchone()
        if catalog != (True,):
            raise RuntimeError("database Supabase auth catalog is incompatible")

        table_cursor = await connection.execute(
            """
            SELECT required.table_name,
                   relation.oid IS NOT NULL AS relation_exists,
                   COALESCE(relation.relrowsecurity, false) AS rls_enabled,
                   COALESCE(relation.relforcerowsecurity, false) AS rls_forced,
                   COALESCE(
                       has_table_privilege(current_user, relation.oid, 'SELECT'), false
                   ) AS can_select,
                   COALESCE(
                       has_table_privilege(current_user, relation.oid, 'INSERT'), false
                   ) AS can_insert,
                   COALESCE(
                       has_table_privilege(current_user, relation.oid, 'UPDATE'), false
                   ) AS can_update,
                   COALESCE(
                       has_table_privilege(current_user, relation.oid, 'DELETE'), false
                   ) AS can_delete,
                   COALESCE(
                       has_table_privilege(current_user, relation.oid, 'TRUNCATE'), false
                   ) AS can_truncate
            FROM unnest(%s::text[]) AS required(table_name)
            LEFT JOIN pg_catalog.pg_class AS relation
              ON relation.oid = to_regclass('public.' || required.table_name)
             AND relation.relkind IN ('r', 'p')
            """,
            (list(_GOVERNED_TABLE_PRIVILEGES),),
        )
        table_rows = await table_cursor.fetchall()
        tables = {row[0]: row for row in table_rows}
        if set(tables) != set(_GOVERNED_TABLE_PRIVILEGES):
            raise RuntimeError("database governed-table migration is incomplete")
        privilege_names = ("select", "insert", "update", "delete", "truncate")
        for table_name, expected_privileges in _GOVERNED_TABLE_PRIVILEGES.items():
            row = tables[table_name]
            if row[1] is not True or row[2] is not True or row[3] is not True:
                raise RuntimeError(
                    f"database governed table {table_name} must exist with forced RLS"
                )
            actual_privileges = frozenset(
                name for name, granted in zip(privilege_names, row[4:], strict=True) if granted
            )
            if actual_privileges != expected_privileges:
                raise RuntimeError(
                    f"database governed table {table_name} has invalid runtime privileges"
                )

        sequence_cursor = await connection.execute(
            """
            SELECT required.sequence_name,
                   sequence.oid IS NOT NULL AS sequence_exists,
                   COALESCE(
                       has_sequence_privilege(current_user, sequence.oid, 'USAGE'), false
                   ) AS can_usage,
                   COALESCE(
                       has_sequence_privilege(current_user, sequence.oid, 'SELECT'), false
                   ) AS can_select,
                   COALESCE(
                       has_sequence_privilege(current_user, sequence.oid, 'UPDATE'), false
                   ) AS can_update
            FROM unnest(%s::text[]) AS required(sequence_name)
            LEFT JOIN pg_catalog.pg_class AS sequence
              ON sequence.oid = to_regclass('public.' || required.sequence_name)
             AND sequence.relkind = 'S'
            """,
            (list(_GOVERNED_SEQUENCES),),
        )
        sequence_rows = await sequence_cursor.fetchall()
        sequences = {row[0]: row for row in sequence_rows}
        if set(sequences) != set(_GOVERNED_SEQUENCES):
            raise RuntimeError("database governed-sequence migration is incomplete")
        for sequence_name in _GOVERNED_SEQUENCES:
            row = sequences[sequence_name]
            if row[1:] != (True, True, True, False):
                raise RuntimeError(
                    f"database sequence {sequence_name} has invalid runtime privileges"
                )

        attribution_acl_cursor = await connection.execute(
            """
            SELECT
                procedure.oid IS NOT NULL AS attribution_function_exists,
                COALESCE(
                    (
                        SELECT bool_or(
                            acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'
                        )
                        FROM aclexplode(
                            COALESCE(procedure.proacl, acldefault('f', procedure.proowner))
                        ) AS acl
                    ),
                    false
                ) AS attribution_function_public_execute
            FROM (SELECT 1) AS required
            LEFT JOIN pg_catalog.pg_proc AS procedure
              ON procedure.oid = to_regprocedure(
                  'public.anc_validate_mcp_tool_call_attribution()'
              )
            """
        )
        attribution_acl = await attribution_acl_cursor.fetchone()
        if (
            attribution_acl is None
            or attribution_acl[0] is not True
            or attribution_acl[1] is not False
        ):
            raise RuntimeError("attribution trigger function must deny PUBLIC EXECUTE")

        # Same pool connection is the Railway runtime role; pg_catalog reads
        # are allowed, supabase_migrations is not.
        migration_report = await assess_database_migrations(connection)
        if not migration_report.ready:
            LOGGER.error("%s", migration_report.message)
        await _warn_expiring_agent_runs(connection)


def _unavailable_payload(
    *,
    catalog_compatible: bool | None = None,
    migration_report: MigrationReadinessReport | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"status": "unavailable"}
    if migration_report is not None and not migration_report.ready:
        payload.update(migration_report.as_ready_payload())
        if catalog_compatible is False:
            payload["auth_catalog_compatible"] = False
        return payload
    if catalog_compatible is False:
        payload["reason"] = "supabase_auth_catalog_incompatible"
    return payload


async def _warn_expiring_agent_runs(connection: object) -> None:
    """Log upcoming run expiry without affecting readiness or leaking subjects."""

    try:
        runs = await fetch_expiring_agent_runs(connection)
    except Exception:
        LOGGER.warning("agent run expiry probe failed")
        return
    message = format_expiring_agent_run_warning(runs)
    if message is not None:
        LOGGER.warning("%s", message)


async def _probe_expiring_agent_runs_after_ready(
    pool: SharedPool,
    readiness: _Readiness,
    *,
    timeout_seconds: float,
    now: datetime | None = None,
) -> None:
    """Warn about upcoming expiry without sharing the readiness-critical timeout."""

    observed = datetime.now(UTC) if now is None else now
    last = readiness.last_expiry_warning_at
    if last is not None and (observed - last).total_seconds() < (
        AGENT_RUN_EXPIRY_WARNING_INTERVAL_SECONDS
    ):
        return
    try:
        async with asyncio.timeout(timeout_seconds), pool.connection() as connection:
            await _warn_expiring_agent_runs(connection)
    except Exception:
        LOGGER.warning("agent run expiry probe failed")
    readiness.last_expiry_warning_at = observed


async def _live_readiness_body(
    pool: SharedPool,
    *,
    timeout_seconds: float,
    readiness: _Readiness,
) -> dict[str, object]:
    catalog_compatible = False
    migration_report: MigrationReadinessReport | None = None
    try:
        async with asyncio.timeout(timeout_seconds), pool.connection() as connection:
            cursor = await connection.execute("SELECT public.anc_auth_catalog_compatible()")
            catalog_compatible = await cursor.fetchone() == (True,)
            migration_report = await assess_database_migrations(connection)
    except Exception:
        return {"status": "unavailable"}
    if catalog_compatible and migration_report is not None and migration_report.ready:
        await _probe_expiring_agent_runs_after_ready(
            pool,
            readiness,
            timeout_seconds=timeout_seconds,
        )
        return {"status": "ready"}
    return _unavailable_payload(
        catalog_compatible=catalog_compatible,
        migration_report=migration_report,
    )


def _add_health_routes(
    app: Starlette,
    readiness: _Readiness,
    pool: SharedPool,
    *,
    timeout_seconds: float,
) -> None:
    async def health(_request: Request) -> JSONResponse:
        return JSONResponse(
            {"status": "ok"},
            headers={"Cache-Control": "no-store"},
        )

    async def ready(_request: Request) -> JSONResponse:
        if not readiness.ready:
            return JSONResponse(
                {"status": "unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        body = await _live_readiness_body(
            pool,
            timeout_seconds=timeout_seconds,
            readiness=readiness,
        )
        status_code = 200 if body.get("status") == "ready" else 503
        return JSONResponse(
            body,
            status_code=status_code,
            headers={"Cache-Control": "no-store"},
        )

    app.router.routes.extend(
        (
            Route("/health", health, methods=["GET"], name="health"),
            Route("/ready", ready, methods=["GET"], name="ready"),
        )
    )


def build_hosted_app(
    settings: HostedSettings,
    *,
    pool: SharedPool | None = None,
    repository_factory: RepositoryFactory = _default_repository,
    identity_repository_factory: IdentityRepositoryFactory = _default_identity_repository,
    audit_sink_factory: AuditSinkFactory = _default_audit_sink,
    consent_registrar: ConsentRegistrar = _default_consent_registrar,
) -> Starlette:
    """Compose the production ASGI app without opening network or database I/O."""

    if not isinstance(settings, HostedSettings):
        raise TypeError("settings must be HostedSettings")
    shared_pool = (
        pool
        if pool is not None
        else AsyncConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
            open=False,
            timeout=float(settings.database_startup_timeout_seconds),
            name="ai-nature-company",
        )
    )
    repository = repository_factory(shared_pool)
    identity_repository = identity_repository_factory(shared_pool)
    audit_sink = audit_sink_factory(shared_pool)
    service = create_service(store=repository)
    token_verifier = SupabaseJWTVerifier(settings.jwt_settings)
    authenticate_token = SupabaseIdentityAuthenticator(
        identity_repository,
        resource_server_url=settings.public_mcp_url,
    )
    readiness = _Readiness()

    @asynccontextmanager
    async def lifespan(_server: object):
        try:
            await shared_pool.open(
                wait=True,
                timeout=float(settings.database_startup_timeout_seconds),
            )
            await _database_preflight(
                shared_pool,
                runtime_role=settings.runtime_database_role,
            )
            readiness.last_expiry_warning_at = datetime.now(UTC)
            readiness.ready = True
            yield {"database": "ready"}
        finally:
            readiness.ready = False
            if not getattr(shared_pool, "closed", True):
                await shared_pool.close()

    app = build_http_app(
        service,
        token_verifier=token_verifier,
        authenticate_token=authenticate_token,
        settings=settings.http_settings,
        lifespan=lifespan,
        call_audit_sink=audit_sink,
        configure_server=lambda server: consent_registrar(server, settings),
    )
    _add_health_routes(
        app,
        readiness,
        shared_pool,
        timeout_seconds=float(
            min(
                settings.database_startup_timeout_seconds,
                DEFAULT_DATABASE_READINESS_TIMEOUT_SECONDS,
            )
        ),
    )
    app.state.hosted_runtime = HostedRuntime(
        pool=shared_pool,
        repository=repository,
        identity_repository=identity_repository,
        audit_sink=audit_sink,
        service=service,
        token_verifier=token_verifier,
        authenticate_token=authenticate_token,
    )
    # The built-in route is public configuration, and this exact URL must also
    # be configured as the authorization path in Supabase OAuth Server.
    app.state.supabase_consent_ui_url = settings.supabase_consent_ui_url
    app.add_middleware(QueryFreeAccessLogMiddleware)
    return app


def create_app() -> Starlette:
    """Uvicorn application factory backed only by trusted process environment."""

    return build_hosted_app(HostedSettings.from_environ(os.environ))


def main() -> None:
    """Run the production HTTP process; TLS terminates at the deployment ingress."""

    import uvicorn

    # Validate all authority and public-resource settings before starting worker
    # processes.  The Uvicorn factory validates again inside each worker.
    HostedSettings.from_environ(os.environ)
    port = _bounded_integer_environment(
        os.environ,
        "PORT",
        default=DEFAULT_PORT,
        minimum=1,
        maximum=65_535,
    )
    workers = _bounded_integer_environment(
        os.environ,
        "WEB_CONCURRENCY",
        default=DEFAULT_WORKERS,
        minimum=1,
        maximum=32,
    )
    host = _canonical_environment_value(os.environ, "HOST", required=False) or DEFAULT_HOST
    uvicorn.run(
        "ai_nature_company.hosted:create_app",
        factory=True,
        host=host,
        port=port,
        workers=workers,
        proxy_headers=True,
        server_header=False,
        access_log=False,
    )


__all__ = [
    "HostedRuntime",
    "HostedSettings",
    "build_hosted_app",
    "create_app",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised by the console entry point
    main()
