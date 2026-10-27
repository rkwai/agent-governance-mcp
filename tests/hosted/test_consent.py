"""Contracts for the built-in Supabase OAuth authorization UI."""

from __future__ import annotations

import base64
import json
import re

import pytest
from mcp.server import MCPServer
from starlette.testclient import TestClient

from ai_nature_company.consent import (
    CONSENT_JAVASCRIPT_PATH,
    CONSENT_PATH,
    CONSENT_STYLESHEET_PATH,
    GROKBOT_RETURN_HOST,
    GROKBOT_RETURN_PATH,
    GROKBOT_RETURN_SCHEME,
    LOOPBACK_HTTP_HOSTS,
    LOOPBACK_HTTP_PATH,
    LOOPBACK_HTTP_PORT,
    MAX_RETURN_ADDRESS_LENGTH,
    STANDARD_CONSENT_SCOPES,
    ConsentSettings,
    consent_html,
    consent_javascript,
    is_allowed_consent_return_address,
    normalized_consent_scopes,
    register_consent_routes,
)


def _settings(**changes: str) -> ConsentSettings:
    values = {
        "supabase_url": "https://project.supabase.co",
        "supabase_publishable_key": "sb_publishable_public-browser-key",
    }
    values.update(changes)
    return ConsentSettings(**values)


def _client(settings: ConsentSettings | None = None) -> TestClient:
    server = MCPServer("consent-contract")
    register_consent_routes(server, settings or _settings())
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )
    return TestClient(app)


def _legacy_key(role: str, *, algorithm: str = "HS256") -> str:
    def segment(value: object) -> str:
        encoded = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode()

    return f"{segment({'alg': algorithm, 'typ': 'JWT'})}.{segment({'role': role})}.signature"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("supabase_url", "", "canonical HTTPS origin"),
        ("supabase_url", "http://project.supabase.co", "HTTPS origin"),
        ("supabase_url", "https://project.supabase.co/\x7f", "control characters"),
        ("supabase_url", "https://user@project.supabase.co", "user information"),
        ("supabase_url", "https://project.supabase.co/auth/v1", "origin without a path"),
        ("supabase_url", "https://project.supabase.co?x=1", "query or fragment"),
        ("supabase_url", "https://project.supabase.co:invalid", "valid optional port"),
        ("supabase_publishable_key", "", "must not be empty"),
        ("supabase_publishable_key", " public-key", "canonical"),
        ("supabase_publishable_key", "public\nkey", "control characters"),
        ("supabase_publishable_key", "x" * 8193, "must not exceed"),
    ],
)
def test_consent_settings_reject_noncanonical_public_configuration(
    field: str,
    value: str,
    message: str,
) -> None:
    values = {
        "supabase_url": "https://project.supabase.co",
        "supabase_publishable_key": "sb_publishable_public-browser-key",
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        ConsentSettings(**values)


@pytest.mark.parametrize(
    "key",
    [
        "sb_secret_do-not-accept",
        _legacy_key("service_role"),
        _legacy_key("supabase_admin"),
        _legacy_key("authenticated"),
        _legacy_key("anon", algorithm="none"),
        "not-a-supabase-browser-key",
        "aaa.not-base64!.signature",
    ],
)
def test_consent_settings_reject_secret_privileged_or_unknown_key_forms(key: str) -> None:
    with pytest.raises(ValueError) as raised:
        _settings(supabase_publishable_key=key)

    assert key not in str(raised.value)


@pytest.mark.parametrize(
    "key",
    [
        "sb_publishable_browser-safe-value",
        _legacy_key("anon"),
    ],
)
def test_consent_settings_accept_only_documented_public_key_forms(key: str) -> None:
    settings = _settings(supabase_publishable_key=key)

    assert settings.supabase_publishable_key == key


def test_consent_html_escapes_public_configuration_and_has_no_inline_script() -> None:
    settings = _settings()
    # Exercise the output encoder even if a caller circumvents the frozen settings
    # invariant; accepted key forms themselves intentionally contain no HTML metacharacters.
    object.__setattr__(settings, "supabase_publishable_key", 'public"<key&')

    page = consent_html(settings, "auth_request-123")

    assert 'data-publishable-key="public&quot;&lt;key&amp;"' in page
    assert 'data-authorization-id="auth_request-123"' in page
    assert 'public"<key&' not in page
    assert '<script type="module" src="/oauth/consent/app.js"></script>' in page
    assert not re.search(r"<script(?:\s[^>]*)?>\s*[^<\s]", page)
    assert "style=" not in page


def test_consent_html_rejects_unvalidated_inputs() -> None:
    with pytest.raises(TypeError, match="settings must be ConsentSettings"):
        consent_html(object(), "request-1")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="authorization_id is invalid"):
        consent_html(_settings(), "request with spaces")


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?authorization_id=",
        "?authorization_id=bad%20request",
        "?authorization_id=%3Cscript%3Ealert(1)%3C%2Fscript%3E",
        f"?authorization_id={'a' * 257}",
        "?authorization_id=valid&authorization_id=also-valid",
    ],
)
def test_consent_endpoint_rejects_missing_malformed_or_ambiguous_authorization_id(
    query: str,
) -> None:
    with _client() as client:
        response = client.get(f"{CONSENT_PATH}{query}")

    assert response.status_code == 400
    assert response.text == "Invalid or missing authorization request."
    assert "script" not in response.text.lower()
    _assert_security_headers(response.headers)


def test_consent_endpoint_serves_a_content_minimized_login_and_review_page() -> None:
    with _client() as client:
        response = client.get(f"{CONSENT_PATH}?authorization_id=request_A-123")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'data-authorization-id="request_A-123"' in response.text
    assert 'autocomplete="username"' in response.text
    assert 'autocomplete="current-password"' in response.text
    assert "pre-provisioned agent account" in response.text
    assert "service_role" not in response.text
    _assert_security_headers(response.headers)


def test_static_routes_are_external_content_with_the_same_security_headers() -> None:
    with _client() as client:
        script = client.get(CONSENT_JAVASCRIPT_PATH)
        stylesheet = client.get(CONSENT_STYLESHEET_PATH)

    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert script.text == consent_javascript()
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert stylesheet.text
    _assert_security_headers(script.headers)
    _assert_security_headers(stylesheet.headers)


def test_browser_flow_sends_credentials_only_to_supabase_and_keeps_only_access_token() -> None:
    script = consent_javascript()

    assert "/auth/v1/token?grant_type=password" in script
    assert "/auth/v1/oauth/authorizations/" in script
    assert "sessionStorage" in script
    assert "localStorage" not in script
    assert "refresh_token" not in script
    assert "innerHTML" not in script
    assert "credentials" not in script
    assert "location.replace(result.redirect_url)" in script
    assert "redirect_url=params" not in script
    assert "sb_publishable_public-browser-key" not in script
    assert "service_role" not in script


def test_consent_server_accepts_no_credentials_or_decisions() -> None:
    with _client() as client:
        response = client.post(
            CONSENT_PATH,
            data={"email": "agent@example.test", "password": "never-send-here"},
        )

    assert response.status_code == 405
    assert response.text == "Method not allowed."
    assert response.headers["allow"] == "GET, HEAD"
    assert "never-send-here" not in response.text
    _assert_security_headers(response.headers)


@pytest.mark.parametrize("path", [CONSENT_JAVASCRIPT_PATH, CONSENT_STYLESHEET_PATH])
def test_static_consent_assets_keep_security_headers_on_method_not_allowed(path: str) -> None:
    with _client() as client:
        response = client.post(path, content="not-used")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    _assert_security_headers(response.headers)


def test_unknown_consent_asset_keeps_security_headers_on_not_found() -> None:
    with _client() as client:
        response = client.get(f"{CONSENT_PATH}/missing.js")

    assert response.status_code == 404
    assert response.text == "Consent asset not found."
    _assert_security_headers(response.headers)


def test_route_registration_rejects_invalid_dependencies() -> None:
    with pytest.raises(TypeError, match="server must be MCPServer"):
        register_consent_routes(object(), _settings())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="settings must be ConsentSettings"):
        register_consent_routes(MCPServer("test"), object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "scopes",
    [
        "openid",
        "openid offline_access",
        "openid email profile phone offline_access",
        ["openid", "offline_access"],
        ("offline_access", "openid"),
    ],
)
def test_normalized_consent_scopes_accept_standard_oidc_including_offline_access(
    scopes: str | list[str] | tuple[str, ...],
) -> None:
    normalized = normalized_consent_scopes(scopes)

    assert normalized
    assert all(scope in STANDARD_CONSENT_SCOPES for scope in normalized)
    if "offline_access" in scopes:
        assert "offline_access" in normalized


@pytest.mark.parametrize(
    "scopes",
    [
        "",
        "   ",
        "mcp:admin",
        "openid mcp:admin",
        ["openid", "write:all"],
        [" openid"],
        12,
        None,
        {"openid"},
    ],
)
def test_normalized_consent_scopes_reject_empty_custom_or_noncanonical_values(
    scopes: object,
) -> None:
    with pytest.raises(ValueError, match="unsupported scopes"):
        normalized_consent_scopes(scopes)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.cursor.com/agents/mcp/oauth/callback",
        "https://www.cursor.com/agents/mcp/oauth/callback?code=x&state=y",
        "http://localhost:8787/callback",
        "http://LOCALHOST:8787/callback",
        "http://localhost:8787/callback?code=x&state=y",
        "http://127.0.0.1:8787/callback",
        "http://127.0.0.1:8787/callback?code=x",
        "grokbot://mcp/oauth/callback",
        "grokbot://mcp/oauth/callback?code=x&state=y",
        "GROKBOT://MCP/oauth/callback",
    ],
)
def test_consent_return_address_allows_https_loopback_http_and_grokbot(url: str) -> None:
    assert is_allowed_consent_return_address(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/callback",
        "http://example.com:8787/callback",
        "http://evil.localhost:8787/callback",
        "http://localhost.evil.test:8787/callback",
        "http://127.0.0.2:8787/callback",
        "http://0.0.0.0:8787/callback",
        "http://[::1]:8787/callback",
        "http://localhost/callback",
        "http://localhost:80/callback",
        "http://localhost:8787/",
        "http://localhost:8787/callback/extra",
        "http://127.0.0.1:8788/callback",
        "http://127.1:8787/callback",
        "http://localhost:notaport/callback",
    ],
)
def test_consent_return_address_rejects_random_http_hosts(url: str) -> None:
    assert is_allowed_consent_return_address(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hello",
        "file:///etc/passwd",
        "about:blank",
        "blob:https://www.cursor.com/uuid",
        "myapp://mcp/oauth/callback",
        "grok://mcp/oauth/callback",
        "grokbot://other/oauth/callback",
        "grokbot://mcp/oauth/callback/extra",
        "grokbot://mcp/OAuth/callback",
        "grokbot://mcp:443/oauth/callback",
        "https://user@www.cursor.com/agents/mcp/oauth/callback",
        "https://user:pass@www.cursor.com/agents/mcp/oauth/callback",
        "http://user@localhost:8787/callback",
        "http://user:pass@127.0.0.1:8787/callback",
        "grokbot://user@mcp/oauth/callback",
        "https://",
        "not a url",
        "",
        "https://www.cursor.com/\x7f",
        "x" * (MAX_RETURN_ADDRESS_LENGTH + 1),
        None,
        12,
    ],
)
def test_consent_return_address_rejects_unsafe_schemes_userinfo_and_open_redirects(
    url: object,
) -> None:
    assert is_allowed_consent_return_address(url) is False


def test_consent_javascript_embeds_generated_scope_and_return_allowlists() -> None:
    script = consent_javascript()
    scopes = ",".join(f"'{scope}'" for scope in STANDARD_CONSENT_SCOPES)
    hosts = ",".join(f"'{host}'" for host in LOOPBACK_HTTP_HOSTS)

    assert "offline_access" in STANDARD_CONSENT_SCOPES
    assert f"const standardScopes=new Set([{scopes}]);" in script
    assert f"const loopbackHttpHosts=new Set([{hosts}]);" in script
    assert "function allowedReturnAddress(destination){" in script
    assert "if(!allowedReturnAddress(destination))" in script
    assert "destination.protocol==='https:'" in script
    assert f"destination.protocol==='{GROKBOT_RETURN_SCHEME}:'" in script
    assert f"destination.hostname==='{GROKBOT_RETURN_HOST}'" in script
    assert f"destination.pathname==='{GROKBOT_RETURN_PATH}'" in script
    assert f"destination.port==='{LOOPBACK_HTTP_PORT}'" in script
    assert f"destination.pathname==='{LOOPBACK_HTTP_PATH}'" in script
    assert f"result.redirect_url.length>{MAX_RETURN_ADDRESS_LENGTH}" in script
    assert "console." not in script
    assert "document.cookie" not in script
    assert "authorization_id=" not in script
    assert "searchParams" not in script
    assert "location.search" not in script


def _assert_security_headers(headers: object) -> None:
    assert headers["cache-control"] == "no-store"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "connect-src https://project.supabase.co" in csp
