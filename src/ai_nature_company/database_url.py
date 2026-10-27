"""Fail-closed PostgreSQL URI validation shared by production database clients."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

_QUERY_AUTHORITY_OVERRIDES = frozenset(
    {
        "dbname",
        "host",
        "hostaddr",
        "password",
        "passfile",
        "port",
        "service",
        "servicefile",
        "sslcert",
        "sslkey",
        "sslpassword",
        "user",
    }
)


def require_tls_database_url(value: str, *, field_name: str = "DATABASE_URL") -> str:
    """Require a hosted libpq URI with hostname verification and an explicit CA."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be one canonical PostgreSQL URI")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} must not contain control characters")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be a PostgreSQL URI with a host") from error
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError(f"{field_name} must be a PostgreSQL URI with a host")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{field_name} must contain a valid optional port") from error
    if port == 6543:
        raise ValueError(f"{field_name} must not use a transaction-pooler port")
    if parsed.fragment:
        raise ValueError(f"{field_name} must not contain a fragment")
    try:
        parameters = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise ValueError(f"{field_name} contains invalid connection parameters") from error
    by_name: dict[str, list[str]] = {}
    for name, parameter_value in parameters:
        by_name.setdefault(name, []).append(parameter_value)
    forbidden_overrides = sorted(_QUERY_AUTHORITY_OVERRIDES.intersection(by_name))
    if forbidden_overrides:
        names = ", ".join(forbidden_overrides)
        raise ValueError(
            f"{field_name} must not override connection authority in its query: {names}"
        )
    if by_name.get("sslmode") != ["verify-full"]:
        raise ValueError(f"{field_name} must set sslmode=verify-full exactly once")
    root_certificates = by_name.get("sslrootcert")
    if root_certificates is None or len(root_certificates) != 1:
        raise ValueError(f"{field_name} must set sslrootcert exactly once")
    root_certificate = root_certificates[0]
    if root_certificate != "system" and not root_certificate.startswith("/"):
        raise ValueError(f"{field_name} sslrootcert must be system or an absolute file path")
    return value


__all__ = ["require_tls_database_url"]
