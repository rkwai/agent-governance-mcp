"""Trusted stdio startup configuration tests."""

from __future__ import annotations

import asyncio

import pytest

import ai_nature_company.__main__ as entrypoint
import ai_nature_company.postgres as postgres_module
import ai_nature_company.service as service_module
import ai_nature_company.store as store_module
from ai_nature_company.__main__ import _stdio_authority_from_environment
from ai_nature_company.auth import Role, is_verified_principal


def test_stdio_authority_is_issued_from_required_server_configuration() -> None:
    authority = _stdio_authority_from_environment(
        {
            "AI_NATURE_PRINCIPAL_ID": "founder-1",
            "AI_NATURE_COMPANY_ID": "company-1",
            "AI_NATURE_ROLE_GRANTS": '["founder", "observer"]',
        }
    )

    assert is_verified_principal(authority)
    assert authority.principal_id == "founder-1"
    assert authority.company_id == "company-1"
    assert authority.roles == {Role.FOUNDER, Role.OBSERVER}
    assert authority.authentication_method == "local-test"


@pytest.mark.parametrize(
    "missing",
    ["AI_NATURE_PRINCIPAL_ID", "AI_NATURE_COMPANY_ID", "AI_NATURE_ROLE_GRANTS"],
)
def test_stdio_authority_rejects_missing_configuration(missing: str) -> None:
    environ = {
        "AI_NATURE_PRINCIPAL_ID": "founder-1",
        "AI_NATURE_COMPANY_ID": "company-1",
        "AI_NATURE_ROLE_GRANTS": '["founder"]',
    }
    del environ[missing]

    with pytest.raises(RuntimeError, match=f"{missing} is required"):
        _stdio_authority_from_environment(environ)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not-json", "must be a JSON array"),
        ("[]", "must be a non-empty JSON array"),
        ('{"role":"founder"}', "must be a non-empty JSON array"),
        ("[1]", "entries must be role strings"),
        ('["superuser"]', "contains an unknown role"),
        ('["founder", "founder"]', "must not contain duplicate roles"),
    ],
)
def test_stdio_authority_rejects_invalid_role_configuration(raw: str, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        _stdio_authority_from_environment(
            {
                "AI_NATURE_PRINCIPAL_ID": "founder-1",
                "AI_NATURE_COMPANY_ID": "company-1",
                "AI_NATURE_ROLE_GRANTS": raw,
            }
        )


def test_main_wires_postgres_service_and_runs_stdio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Repository:
        async def open(self) -> None:
            calls.append(("repository", "opened"))

        async def close(self) -> None:
            calls.append(("repository", "closed"))

    repository = _Repository()
    service = object()
    calls: list[tuple[str, object]] = []

    class _Server:
        def __init__(self, lifespan: object) -> None:
            self.lifespan = lifespan

        def run(self, transport: str) -> None:
            async def exercise_lifespan() -> None:
                async with self.lifespan(None):  # type: ignore[operator]
                    calls.append(("lifespan", "active"))

            asyncio.run(exercise_lifespan())
            calls.append(("run", transport))

    def postgres_repository(database_url: str) -> object:
        calls.append(("database_url", database_url))
        return repository

    def create_service(*, store: object | None = None) -> object:
        calls.append(("store", store))
        return service

    def build_server(
        received_service: object,
        resolver: object,
        *,
        lifespan: object,
    ) -> _Server:
        assert received_service is service
        assert is_verified_principal(resolver.authority)  # type: ignore[attr-defined]
        assert callable(lifespan)
        calls.append(("principal", resolver.authority.principal_id))  # type: ignore[attr-defined]
        return _Server(lifespan)

    monkeypatch.setenv("AI_NATURE_PRINCIPAL_ID", "founder-1")
    monkeypatch.setenv("AI_NATURE_COMPANY_ID", "company-1")
    monkeypatch.setenv("AI_NATURE_ROLE_GRANTS", '["founder"]')
    monkeypatch.setenv("AI_NATURE_DATABASE_URL", "postgresql://configured-at-runtime")
    monkeypatch.setattr(postgres_module, "PostgresRepository", postgres_repository)
    monkeypatch.setattr(service_module, "create_service", create_service)
    monkeypatch.setattr(entrypoint, "build_server", build_server)

    entrypoint.main()

    assert calls == [
        ("database_url", "postgresql://configured-at-runtime"),
        ("store", repository),
        ("principal", "founder-1"),
        ("repository", "opened"),
        ("lifespan", "active"),
        ("repository", "closed"),
        ("run", "stdio"),
    ]


def test_main_requires_explicit_local_mode_without_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_NATURE_PRINCIPAL_ID", "founder-1")
    monkeypatch.setenv("AI_NATURE_COMPANY_ID", "company-1")
    monkeypatch.setenv("AI_NATURE_ROLE_GRANTS", '["founder"]')
    monkeypatch.delenv("AI_NATURE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AI_NATURE_LOCAL_MODE", raising=False)

    with pytest.raises(RuntimeError, match="AI_NATURE_LOCAL_MODE=1"):
        entrypoint.main()


def test_main_uses_memory_only_in_explicit_local_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = object()
    service = object()
    calls: list[tuple[str, object]] = []

    class _Server:
        def __init__(self, lifespan: object) -> None:
            self.lifespan = lifespan

        def run(self, transport: str) -> None:
            async def exercise_lifespan() -> None:
                async with self.lifespan(None):  # type: ignore[operator]
                    calls.append(("lifespan", "active"))

            asyncio.run(exercise_lifespan())
            calls.append(("run", transport))

    def create_service(*, store: object | None = None) -> object:
        calls.append(("store", store))
        return service

    def build_server(
        received_service: object,
        resolver: object,
        *,
        lifespan: object,
    ) -> _Server:
        assert received_service is service
        assert callable(lifespan)
        calls.append(("principal", resolver.authority.principal_id))  # type: ignore[attr-defined]
        return _Server(lifespan)

    monkeypatch.setenv("AI_NATURE_PRINCIPAL_ID", "founder-1")
    monkeypatch.setenv("AI_NATURE_COMPANY_ID", "company-1")
    monkeypatch.setenv("AI_NATURE_ROLE_GRANTS", '["founder"]')
    monkeypatch.setenv("AI_NATURE_LOCAL_MODE", "1")
    monkeypatch.delenv("AI_NATURE_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(store_module, "InMemoryStore", lambda: repository)
    monkeypatch.setattr(service_module, "create_service", create_service)
    monkeypatch.setattr(entrypoint, "build_server", build_server)

    entrypoint.main()

    assert calls == [
        ("store", repository),
        ("principal", "founder-1"),
        ("lifespan", "active"),
        ("run", "stdio"),
    ]
