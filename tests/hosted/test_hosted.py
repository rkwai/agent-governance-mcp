"""Contracts for the production Streamable HTTP process composition."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any

import pytest
from starlette.applications import Starlette

from ai_nature_company.attribution import ToolCallAuditEvent
from ai_nature_company.hosted import (
    HostedSettings,
    _live_readiness_body,
    _Readiness,
    build_hosted_app,
)
from ai_nature_company.migration_readiness import REQUIRED_SCHEMA_FACTS


def _environment(**changes: str) -> dict[str, str]:
    values = {
        "DATABASE_URL": (
            "postgresql://runtime:super-secret@db.example.test:5432/anc"
            "?sslmode=verify-full&sslrootcert=system"
        ),
        "ANC_SUPABASE_URL": "https://project-ref.supabase.co",
        "ANC_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_public-test-value",
        "ANC_SUPABASE_ISSUER": "https://project-ref.supabase.co/auth/v1",
        "ANC_SUPABASE_AUDIENCE": "authenticated",
        "ANC_PUBLIC_MCP_URL": "https://substrate.example.test/mcp",
        "ANC_SUPABASE_CLIENT_IDS": "grok-connector,chatgpt-connector",
    }
    values.update(changes)
    return values


def test_hosted_settings_build_exact_auth_and_http_configuration() -> None:
    settings = HostedSettings.from_environ(
        _environment(
            ANC_SUPABASE_JWKS_URL=("https://project-ref.supabase.co/auth/v1/.well-known/jwks.json"),
            ANC_ALLOWED_HOSTS="substrate.example.test,internal-proxy.example.test",
            ANC_ALLOWED_ORIGINS="https://x.com,https://chatgpt.com",
            ANC_DATABASE_POOL_MIN_SIZE="2",
            ANC_DATABASE_POOL_MAX_SIZE="8",
            ANC_DATABASE_STARTUP_TIMEOUT_SECONDS="12",
        )
    )

    assert settings.allowed_client_ids == frozenset({"grok-connector", "chatgpt-connector"})
    assert settings.pool_min_size == 2
    assert settings.pool_max_size == 8
    assert settings.database_startup_timeout_seconds == 12
    assert settings.jwt_settings.issuer == "https://project-ref.supabase.co/auth/v1"
    assert settings.supabase_url == "https://project-ref.supabase.co"
    assert settings.supabase_consent_ui_url == ("https://substrate.example.test/oauth/consent")
    assert settings.jwt_settings.audience == "authenticated"
    assert settings.jwt_settings.resource_server_url == ("https://substrate.example.test/mcp")
    assert settings.http_settings.allowed_hosts == (
        "substrate.example.test",
        "internal-proxy.example.test",
    )
    assert settings.http_settings.allowed_origins == (
        "https://x.com",
        "https://chatgpt.com",
    )
    assert "super-secret" not in repr(settings)
    assert "sb_publishable_public-test-value" not in repr(settings)


def test_hosted_settings_derive_jwks_and_public_host_defaults() -> None:
    settings = HostedSettings.from_environ(_environment())

    assert settings.jwt_settings.jwks_url == (
        "https://project-ref.supabase.co/auth/v1/.well-known/jwks.json"
    )
    assert settings.http_settings.effective_allowed_hosts == ("substrate.example.test",)
    assert settings.http_settings.required_scopes == ("openid",)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"DATABASE_URL": ""}, "DATABASE_URL is required"),
        (
            {"DATABASE_URL": "postgresql://runtime:secret@db.example.test/anc"},
            "sslmode=verify-full",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql://runtime:secret@db.example.test/anc"
                    "?sslmode=require&sslrootcert=system"
                )
            },
            "sslmode=verify-full",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql://runtime:secret@db.example.test/anc?sslmode=verify-full"
                )
            },
            "sslrootcert",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql://runtime:secret@db.example.test/anc"
                    "?sslmode=verify-full&sslrootcert=relative.crt"
                )
            },
            "absolute file path",
        ),
        (
            {
                "DATABASE_URL": (
                    "postgresql://runtime:secret@db.example.test:6543/anc"
                    "?sslmode=verify-full&sslrootcert=system"
                )
            },
            "transaction-pooler",
        ),
        ({"ANC_SUPABASE_URL": ""}, "ANC_SUPABASE_URL is required"),
        ({"ANC_SUPABASE_URL": "https://project-ref.supabase.co/"}, "origin"),
        ({"ANC_SUPABASE_PUBLISHABLE_KEY": ""}, "is required"),
        ({"ANC_SUPABASE_ISSUER": "http://project.supabase.test/auth/v1"}, "HTTPS"),
        (
            {"ANC_SUPABASE_ISSUER": "https://other.supabase.co/auth/v1"},
            "must equal",
        ),
        ({"ANC_PUBLIC_MCP_URL": "https://substrate.example.test/"}, "non-root MCP path"),
        ({"ANC_SUPABASE_JWKS_URL": "http://project.supabase.test/jwks"}, "HTTPS"),
        ({"ANC_SUPABASE_CLIENT_IDS": ""}, "is required"),
        ({"ANC_SUPABASE_CLIENT_IDS": "client-a,client-a"}, "duplicates"),
        ({"ANC_ALLOWED_HOSTS": "*.example.test"}, "wildcards"),
        ({"ANC_ALLOWED_ORIGINS": "https://x.com/path"}, "origin"),
        ({"ANC_RUNTIME_DATABASE_ROLE": "not-a-sql-ident"}, "canonical SQL identifier"),
        ({"ANC_DATABASE_POOL_MIN_SIZE": "0"}, "from 1 through"),
        (
            {
                "ANC_DATABASE_POOL_MIN_SIZE": "9",
                "ANC_DATABASE_POOL_MAX_SIZE": "8",
            },
            "cannot exceed",
        ),
    ],
)
def test_hosted_settings_fail_closed_for_invalid_environment(
    changes: Mapping[str, str],
    message: str,
) -> None:
    with pytest.raises((RuntimeError, ValueError), match=message):
        HostedSettings.from_environ(_environment(**changes))


class _Context(AbstractAsyncContextManager[Any]):
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback


class _Pool:
    def __init__(
        self,
        *,
        role_safe: bool = True,
        resolver_exists: bool = True,
        resolver_execute: bool = True,
        catalog_function_exists: bool = True,
        catalog_function_execute: bool = True,
        auth_catalog_compatible: bool = True,
        invalid_table: str | None = None,
        omit_schema_slugs: frozenset[str] = frozenset(),
        extra_forbidden_facts: bool = False,
        applied_names_readable: bool = False,
        applied_names: tuple[str, ...] = (),
        expiring_runs: tuple[tuple[object, ...], ...] = (),
        hang_expiring_probe: bool = False,
    ) -> None:
        self.closed = True
        self.open_calls: list[tuple[bool, float]] = []
        self.close_calls = 0
        self.connection_calls = 0
        self.connection_value = _Connection(
            role_safe=role_safe,
            resolver_exists=resolver_exists,
            resolver_execute=resolver_execute,
            catalog_function_exists=catalog_function_exists,
            catalog_function_execute=catalog_function_execute,
            auth_catalog_compatible=auth_catalog_compatible,
            invalid_table=invalid_table,
            omit_schema_slugs=omit_schema_slugs,
            extra_forbidden_facts=extra_forbidden_facts,
            applied_names_readable=applied_names_readable,
            applied_names=applied_names,
            expiring_runs=expiring_runs,
            hang_expiring_probe=hang_expiring_probe,
        )

    async def open(self, *, wait: bool, timeout: float) -> None:
        self.open_calls.append((wait, timeout))
        self.closed = False

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    def connection(self) -> _Context:
        self.connection_calls += 1
        return _Context(self.connection_value)


class _Cursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None

    async def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.rows)


class _Connection:
    _TABLE_PRIVILEGES: Mapping[str, tuple[bool, bool, bool, bool, bool]] = {
        "anc_stream_heads": (True, True, True, False, False),
        "anc_domain_events": (True, True, False, False, False),
        "anc_audit_events": (True, True, False, False, False),
        "anc_projections": (True, True, True, False, False),
        "anc_idempotency_records": (True, True, True, False, False),
        "anc_budgets": (True, True, True, False, False),
        "anc_budget_reservations": (True, True, True, False, False),
        "anc_work_leases": (True, True, True, False, False),
        "anc_oauth_clients": (False, False, False, False, False),
        "anc_oauth_client_resource_aliases": (False, False, False, False, False),
        "anc_auth_subjects": (False, False, False, False, False),
        "anc_agent_instances": (False, False, False, False, False),
        "anc_agent_runs": (False, False, False, False, False),
        "anc_mcp_tool_calls": (True, True, False, False, False),
    }

    def __init__(
        self,
        *,
        role_safe: bool,
        resolver_exists: bool,
        resolver_execute: bool,
        catalog_function_exists: bool,
        catalog_function_execute: bool,
        auth_catalog_compatible: bool,
        invalid_table: str | None,
        omit_schema_slugs: frozenset[str],
        extra_forbidden_facts: bool,
        applied_names_readable: bool,
        applied_names: tuple[str, ...],
        expiring_runs: tuple[tuple[object, ...], ...] = (),
        hang_expiring_probe: bool = False,
    ) -> None:
        self.role_safe = role_safe
        self.resolver_exists = resolver_exists
        self.resolver_execute = resolver_execute
        self.catalog_function_exists = catalog_function_exists
        self.catalog_function_execute = catalog_function_execute
        self.auth_catalog_compatible = auth_catalog_compatible
        self.invalid_table = invalid_table
        self.omit_schema_slugs = omit_schema_slugs
        self.extra_forbidden_facts = extra_forbidden_facts
        self.applied_names_readable = applied_names_readable
        self.applied_names = applied_names
        self.expiring_runs = expiring_runs
        self.hang_expiring_probe = hang_expiring_probe
        self.autocommit = True
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(
        self,
        query: str,
        parameters: tuple[object, ...] = (),
    ) -> _Cursor:
        self.calls.append((query, parameters))
        if "runtime_role_is_safe" in query:
            flags = (self.role_safe, self.role_safe, True, True, True)
            return _Cursor([flags if self.role_safe else (False, False, True, True, True)])
        if "resolver_exists" in query:
            return _Cursor([(self.resolver_exists, self.resolver_execute)])
        if "compatibility_function_exists" in query:
            return _Cursor([(self.catalog_function_exists, self.catalog_function_execute)])
        if "SELECT public.anc_auth_catalog_compatible()" in query:
            return _Cursor([(self.auth_catalog_compatible,)])
        if "required.table_name" in query:
            names = parameters[0]
            assert isinstance(names, list)
            rows = []
            for name in names:
                privileges = self._TABLE_PRIVILEGES[name]
                exists = name != self.invalid_table
                rows.append((name, exists, exists, exists, *privileges))
            return _Cursor(rows)
        if "required.sequence_name" in query:
            names = parameters[0]
            assert isinstance(names, list)
            return _Cursor([(name, True, True, True, False) for name in names])
        if "attribution_function_public_execute" in query:
            return _Cursor([(True, False)])
        stripped = query.strip()
        if stripped.startswith(("SAVEPOINT ", "ROLLBACK TO SAVEPOINT ", "RELEASE SAVEPOINT ")):
            return _Cursor([])
        if "anc_applied_migration_privilege" in query:
            return _Cursor([(self.applied_names_readable,)])
        if "anc_applied_migration_names" in query:
            if not self.applied_names_readable:
                raise RuntimeError("schema_migrations is not readable")
            return _Cursor([(name,) for name in self.applied_names])
        if "anc_schema_fingerprint" in query:
            return _Cursor(self._schema_fingerprint_rows())
        if "anc_list_expiring_agent_runs" in query:
            if self.hang_expiring_probe:
                await asyncio.sleep(60)
            return _Cursor(list(self.expiring_runs))
        raise AssertionError("unexpected database preflight query")

    def _schema_fingerprint_rows(self) -> list[tuple[object, ...]]:
        rows: list[tuple[object, ...]] = []
        for fact in REQUIRED_SCHEMA_FACTS:
            if not fact.required:
                if self.extra_forbidden_facts:
                    rows.append(
                        (
                            fact.kind,
                            fact.relation or "",
                            fact.name,
                            fact.definition_contains or "",
                        )
                    )
                continue
            if fact.slug in self.omit_schema_slugs:
                continue
            relation = fact.relation or (fact.name if fact.kind == "table" else "")
            rows.append(
                (
                    fact.kind,
                    relation,
                    fact.name,
                    fact.definition_contains or "",
                )
            )
        return rows


class _AuditSink:
    async def record(self, event: ToolCallAuditEvent) -> None:
        del event


class _Response:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        start = next(message for message in messages if message["type"] == "http.response.start")
        self.status_code = start["status"]
        self.body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )

    def json(self) -> object:
        return json.loads(self.body)


async def _get(app: Starlette, path: str) -> _Response:
    delivered = False
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"substrate.example.test")],
        "client": ("127.0.0.1", 12345),
        "server": ("substrate.example.test", 443),
        "state": {},
    }
    await app(scope, receive, send)  # type: ignore[arg-type]
    return _Response(messages)


@pytest.mark.asyncio
async def test_hosted_app_opens_one_shared_pool_and_reports_minimal_health() -> None:
    pool = _Pool()
    settings = HostedSettings.from_environ(_environment())
    app = build_hosted_app(
        settings,
        pool=pool,  # type: ignore[arg-type]
        audit_sink_factory=lambda shared_pool: _AuditSink(),
        consent_registrar=lambda server, hosted_settings: None,
    )

    assert (await _get(app, "/health")).json() == {"status": "ok"}
    unavailable = await _get(app, "/ready")
    assert unavailable.status_code == 503
    assert unavailable.json() == {"status": "unavailable"}

    async with app.router.lifespan_context(app):
        assert pool.open_calls == [(True, 30.0)]
        assert pool.connection_calls == 1
        assert pool.closed is False
        ready = await _get(app, "/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}
        assert pool.connection_calls == 2

    assert pool.closed is True
    assert pool.close_calls == 1
    assert (await _get(app, "/ready")).status_code == 503


def test_hosted_composition_never_opens_database_during_import_or_build() -> None:
    pool = _Pool()

    app = build_hosted_app(
        HostedSettings.from_environ(_environment()),
        pool=pool,  # type: ignore[arg-type]
    )

    assert isinstance(app, Starlette)
    assert pool.open_calls == []
    assert pool.close_calls == 0
    assert app.state.hosted_runtime.pool is pool
    assert app.state.hosted_runtime.repository is not app.state.hosted_runtime.identity_repository
    assert app.state.hosted_runtime.audit_sink is not None
    assert type(app.state.hosted_runtime.audit_sink).__name__ == ("PostgresToolCallAuditSink")
    assert app.state.supabase_consent_ui_url == ("https://substrate.example.test/oauth/consent")
    route_paths = {getattr(route, "path", None) for route in app.routes}
    assert {
        "/oauth/consent",
        "/oauth/consent/app.js",
        "/oauth/consent/styles.css",
    } <= route_paths


@pytest.mark.asyncio
async def test_hosted_builtin_consent_assets_are_public_without_database_io() -> None:
    pool = _Pool()
    app = build_hosted_app(
        HostedSettings.from_environ(_environment()),
        pool=pool,  # type: ignore[arg-type]
    )

    response = await _get(app, "/oauth/consent/styles.css")

    assert response.status_code == 200
    assert pool.open_calls == []
    assert pool.connection_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pool", "message"),
    [
        (_Pool(role_safe=False), "least privilege"),
        (_Pool(resolver_execute=False), "identity resolver"),
        (_Pool(catalog_function_execute=False), "compatibility check"),
        (_Pool(auth_catalog_compatible=False), "auth catalog is incompatible"),
        (_Pool(invalid_table="anc_mcp_tool_calls"), "anc_mcp_tool_calls"),
    ],
)
async def test_hosted_readiness_fails_closed_for_incomplete_database_contract(
    pool: _Pool,
    message: str,
) -> None:
    app = build_hosted_app(
        HostedSettings.from_environ(_environment()),
        pool=pool,  # type: ignore[arg-type]
        audit_sink_factory=lambda shared_pool: _AuditSink(),
        consent_registrar=lambda server, hosted_settings: None,
    )

    with pytest.raises(RuntimeError, match=message):
        async with app.router.lifespan_context(app):
            raise AssertionError("an invalid database contract became ready")

    assert pool.closed is True
    assert pool.close_calls == 1
    assert (await _get(app, "/ready")).status_code == 503


@pytest.mark.asyncio
async def test_hosted_readiness_tracks_live_database_and_auth_catalog_recovery() -> None:
    pool = _Pool()
    app = build_hosted_app(
        HostedSettings.from_environ(_environment()),
        pool=pool,  # type: ignore[arg-type]
        audit_sink_factory=lambda shared_pool: _AuditSink(),
        consent_registrar=lambda server, hosted_settings: None,
    )

    async with app.router.lifespan_context(app):
        assert (await _get(app, "/ready")).status_code == 200

        pool.connection_value.auth_catalog_compatible = False
        unavailable = await _get(app, "/ready")
        assert unavailable.status_code == 503
        assert unavailable.json() == {
            "status": "unavailable",
            "reason": "supabase_auth_catalog_incompatible",
        }

        pool.connection_value.auth_catalog_compatible = True
        assert (await _get(app, "/ready")).status_code == 200


@pytest.mark.asyncio
async def test_hosted_ready_lists_unapplied_migrations_and_stays_alive() -> None:
    pool = _Pool(
        omit_schema_slugs=frozenset({"active_reservation_partial_spend"}),
        extra_forbidden_facts=True,
    )
    app = build_hosted_app(
        HostedSettings.from_environ(_environment()),
        pool=pool,  # type: ignore[arg-type]
        audit_sink_factory=lambda shared_pool: _AuditSink(),
        consent_registrar=lambda server, hosted_settings: None,
    )

    async with app.router.lifespan_context(app):
        assert pool.closed is False
        assert (await _get(app, "/health")).json() == {"status": "ok"}
        unavailable = await _get(app, "/ready")
        assert unavailable.status_code == 503
        body = unavailable.json()
        assert isinstance(body, dict)
        assert body["status"] == "unavailable"
        assert body["reason"] == "database_migrations_unapplied"
        assert body["missing_migrations"] == ["active_reservation_partial_spend"]
        assert (
            "constraint public.anc_budget_reservations."
            "anc_budget_reservations_active_partial_spend_check"
        ) in body["missing_schema_facts"]
        assert ("constraint public.anc_budget_reservations.anc_budget_reservations_check2") in body[
            "forbidden_schema_facts_present"
        ]
        assert "active_reservation_partial_spend" in str(body["message"])


@pytest.mark.asyncio
async def test_hosted_expiry_warning_does_not_fail_ready(
    caplog: pytest.LogCaptureFixture,
) -> None:
    expires_at = datetime.now(UTC) + timedelta(hours=12)
    pool = _Pool(expiring_runs=(("run-soon", "instance-1", expires_at),))
    app = build_hosted_app(
        HostedSettings.from_environ(_environment()),
        pool=pool,  # type: ignore[arg-type]
        audit_sink_factory=lambda shared_pool: _AuditSink(),
        consent_registrar=lambda server, hosted_settings: None,
    )

    with caplog.at_level(logging.WARNING):
        async with app.router.lifespan_context(app):
            ready = await _get(app, "/ready")

    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert "agent runs expire within 72h" in caplog.text
    assert "run-soon" in caplog.text
    assert "instance-1" in caplog.text


@pytest.mark.asyncio
async def test_hosted_expiry_probe_timeout_does_not_fail_ready() -> None:
    pool = _Pool(hang_expiring_probe=True)
    body = await _live_readiness_body(
        pool,  # type: ignore[arg-type]
        timeout_seconds=0.05,
        readiness=_Readiness(ready=True),
    )
    assert body == {"status": "ready"}
    assert pool.connection_calls == 2


@pytest.mark.asyncio
async def test_hosted_app_schedules_no_tick_even_with_legacy_tick_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANC_TICK_ENABLED", "true")
    monkeypatch.setenv("ANC_TICK_MODE", "report")
    pool = _Pool()
    app = build_hosted_app(
        HostedSettings.from_environ(_environment()),
        pool=pool,  # type: ignore[arg-type]
        audit_sink_factory=lambda shared_pool: _AuditSink(),
        consent_registrar=lambda server, hosted_settings: None,
    )
    assert not hasattr(app.state, "tick_settings")
    async with app.router.lifespan_context(app):
        names = {task.get_name() for task in asyncio.all_tasks()}
        assert "anc-report-only-tick" not in names
        assert (await _get(app, "/ready")).status_code == 200
    assert pool.closed is True
