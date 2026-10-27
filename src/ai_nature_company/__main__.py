"""Stdio entry point for the local Phase 1 MCP substrate."""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Mapping
from contextlib import asynccontextmanager

from .auth import (
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
    VerifiedPrincipal,
)
from .server import StaticAuthResolver, build_server


def _required_environment(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise RuntimeError(f"{name} is required for stdio authority")
    return value.strip()


def _role_grants(raw: str, company_id: str) -> tuple[ScopedRoleGrant, ...]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("AI_NATURE_ROLE_GRANTS must be a JSON array of role strings") from error
    if not isinstance(values, list) or not values:
        raise RuntimeError("AI_NATURE_ROLE_GRANTS must be a non-empty JSON array")
    if not all(isinstance(value, str) for value in values):
        raise RuntimeError("AI_NATURE_ROLE_GRANTS entries must be role strings")
    try:
        roles = tuple(Role(value) for value in values)
    except ValueError as error:
        raise RuntimeError("AI_NATURE_ROLE_GRANTS contains an unknown role") from error
    if len(set(roles)) != len(roles):
        raise RuntimeError("AI_NATURE_ROLE_GRANTS must not contain duplicate roles")
    scope = ResourceScope.company(company_id)
    return tuple(ScopedRoleGrant(role, scope) for role in roles)


def _stdio_authority_from_environment(environ: Mapping[str, str]) -> VerifiedPrincipal:
    """Issue one verified stdio principal from trusted process configuration."""

    principal_id = _required_environment(environ, "AI_NATURE_PRINCIPAL_ID")
    company_id = _required_environment(environ, "AI_NATURE_COMPANY_ID")
    grants = _role_grants(
        _required_environment(environ, "AI_NATURE_ROLE_GRANTS"),
        company_id,
    )
    record = PrincipalRecord(principal_id=principal_id, grants=grants)

    # The credential exists only to exercise the same trusted issuance boundary used
    # by local tests.  Stdio transport binds every request to this startup identity.
    credential = secrets.token_hex(32)
    authenticator = LocalTestAuthenticator({credential: record})
    return authenticator.authenticate(credential)


def main() -> None:
    """Construct dependencies from trusted environment and run MCP over stdio."""

    from .postgres import PostgresRepository
    from .service import create_service
    from .store import InMemoryStore

    database_url = os.environ.get("AI_NATURE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if database_url:
        store = PostgresRepository(database_url)
    elif os.environ.get("AI_NATURE_LOCAL_MODE") == "1":
        store = InMemoryStore()
    else:
        raise RuntimeError(
            "AI_NATURE_DATABASE_URL or DATABASE_URL is required unless "
            "AI_NATURE_LOCAL_MODE=1 is explicitly set"
        )
    service = create_service(store=store)
    authority = _stdio_authority_from_environment(os.environ)

    @asynccontextmanager
    async def repository_lifespan(_server):
        if database_url:
            await store.open()
        try:
            yield {}
        finally:
            if database_url:
                await store.close()

    build_server(
        service,
        StaticAuthResolver(authority),
        lifespan=repository_lifespan,
    ).run("stdio")


if __name__ == "__main__":  # pragma: no cover - exercised by the console entry point
    main()
