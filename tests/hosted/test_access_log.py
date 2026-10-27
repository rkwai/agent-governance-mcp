from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

import pytest
from mcp.server import MCPServer
from starlette.testclient import TestClient

from ai_nature_company.access_log import (
    ACCESS_LOGGER_NAME,
    QueryFreeAccessLogMiddleware,
    format_access_line,
    sanitize_access_path,
)
from ai_nature_company.consent import CONSENT_PATH, ConsentSettings, register_consent_routes
from ai_nature_company.hosted import HostedSettings, main


def test_access_line_rejects_query_or_fragment() -> None:
    with pytest.raises(ValueError, match="query or fragment"):
        format_access_line(
            method="GET",
            path="/oauth/consent?authorization_id=secret",
            status_code=200,
            duration_ms=1,
        )


def test_sanitize_access_path_strips_decoded_query_and_fragment() -> None:
    assert sanitize_access_path("/probe#x") == "/probe"
    assert sanitize_access_path("/oauth/consent?authorization_id=secret") == "/oauth/consent"
    assert sanitize_access_path("/probe\n#next") == "/probe"


def test_encoded_fragment_path_is_logged_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def app(scope: Mapping[str, object], receive: object, send: object) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 404, "headers": []})  # type: ignore[misc]
        await send({"type": "http.response.body", "body": b""})  # type: ignore[misc]

    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    async def send(message: Mapping[str, object]) -> None:
        del message

    async def invoke() -> None:
        await QueryFreeAccessLogMiddleware(app)(  # type: ignore[arg-type]
            {"type": "http", "method": "GET", "path": "/probe#x", "raw_path": b"/probe%23x"},
            receive,
            send,
        )

    with caplog.at_level(logging.INFO, logger=ACCESS_LOGGER_NAME):
        asyncio.run(invoke())

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert "path=/probe" in combined
    assert "#" not in combined
    assert "?" not in combined


def test_access_line_is_query_free() -> None:
    line = format_access_line(
        method="GET",
        path="/oauth/consent",
        status_code=200,
        duration_ms=3,
    )
    assert line == "http_access method=GET path=/oauth/consent status=200 duration_ms=3"
    assert "?" not in line
    assert "authorization_id" not in line


def test_consent_authorization_id_cannot_appear_in_application_access_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    distinctive = "authz_canary_9f3c2d1a-should-never-log"
    server = MCPServer("consent-access-log")
    register_consent_routes(
        server,
        ConsentSettings(
            supabase_url="https://project.supabase.co",
            supabase_publishable_key="sb_publishable_public-browser-key",
        ),
    )
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )
    wrapped = QueryFreeAccessLogMiddleware(app)
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    with (
        caplog.at_level(logging.INFO, logger=ACCESS_LOGGER_NAME),
        TestClient(wrapped) as client,  # type: ignore[arg-type]
    ):
        success = client.get(f"{CONSENT_PATH}?authorization_id={distinctive}")
        failure = client.get(f"{CONSENT_PATH}?authorization_id=")

    assert success.status_code == 200
    assert failure.status_code == 400
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert distinctive not in combined
    assert "authorization_id" not in combined
    assert "?" not in combined
    assert any("path=/oauth/consent" in record.getMessage() for record in caplog.records)
    assert logger.name == ACCESS_LOGGER_NAME


def test_hosted_main_disables_uvicorn_access_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://runtime:secret@db.example.test:5432/anc"
        "?sslmode=verify-full&sslrootcert=system",
    )
    monkeypatch.setenv("ANC_SUPABASE_URL", "https://project-ref.supabase.co")
    monkeypatch.setenv("ANC_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_public-test-value")
    monkeypatch.setenv("ANC_SUPABASE_ISSUER", "https://project-ref.supabase.co/auth/v1")
    monkeypatch.setenv("ANC_SUPABASE_AUDIENCE", "authenticated")
    monkeypatch.setenv("ANC_PUBLIC_MCP_URL", "https://substrate.example.test/mcp")
    monkeypatch.setenv("ANC_SUPABASE_CLIENT_IDS", "client-a")
    monkeypatch.setattr("uvicorn.run", fake_run)

    main()

    kwargs = captured["kwargs"]
    assert isinstance(kwargs, Mapping)
    assert kwargs["access_log"] is False
    settings = HostedSettings.from_environ(
        {
            "DATABASE_URL": (
                "postgresql://runtime:secret@db.example.test:5432/anc"
                "?sslmode=verify-full&sslrootcert=system"
            ),
            "ANC_SUPABASE_URL": "https://project-ref.supabase.co",
            "ANC_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_public-test-value",
            "ANC_SUPABASE_ISSUER": "https://project-ref.supabase.co/auth/v1",
            "ANC_SUPABASE_AUDIENCE": "authenticated",
            "ANC_PUBLIC_MCP_URL": "https://substrate.example.test/mcp",
            "ANC_SUPABASE_CLIENT_IDS": "client-a",
        }
    )
    assert settings.runtime_database_role == "ai_nature_company_runtime"
