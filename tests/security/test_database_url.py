from __future__ import annotations

import pytest

from ai_nature_company.database_url import require_tls_database_url


@pytest.mark.parametrize(
    "value",
    [
        (
            "postgresql://operator:secret@db.example.test:5432/company"
            "?sslmode=verify-full&sslrootcert=system"
        ),
        (
            "postgres://operator:secret@db.example.test/company"
            "?application_name=anc&sslmode=verify-full&sslrootcert=/run/secrets/supabase-ca.crt"
        ),
    ],
)
def test_tls_database_url_accepts_verified_postgresql_uris(value: str) -> None:
    assert require_tls_database_url(value) == value


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (
            "mysql://operator@db.example.test/company?sslmode=verify-full&sslrootcert=system",
            "PostgreSQL URI",
        ),
        (
            "postgresql:///company?sslmode=verify-full&sslrootcert=system",
            "with a host",
        ),
        (
            "postgresql://operator@db.example.test:6543/company"
            "?sslmode=verify-full&sslrootcert=system",
            "transaction-pooler",
        ),
        ("postgresql://operator@db.example.test/company", "sslmode=verify-full"),
        (
            "postgresql://operator@db.example.test/company?sslmode=require&sslrootcert=system",
            "sslmode=verify-full",
        ),
        (
            "postgresql://operator@db.example.test/company"
            "?sslmode=verify-full&sslmode=verify-full&sslrootcert=system",
            "exactly once",
        ),
        (
            "postgresql://operator@db.example.test/company?sslmode=verify-full",
            "sslrootcert exactly once",
        ),
        (
            "postgresql://operator@db.example.test/company"
            "?sslmode=verify-full&sslrootcert=system&sslrootcert=system",
            "sslrootcert exactly once",
        ),
        (
            "postgresql://operator@db.example.test/company"
            "?sslmode=verify-full&sslrootcert=relative.crt",
            "absolute file path",
        ),
        (
            "postgresql://operator@db.example.test/company"
            "?sslmode=verify-full&sslrootcert=system#fragment",
            "fragment",
        ),
        (
            "postgresql://operator@db.example.test/company\n"
            "?sslmode=verify-full&sslrootcert=system",
            "control characters",
        ),
        (
            "postgresql://operator@db.example.test/company"
            "?port=6543&sslmode=verify-full&sslrootcert=system",
            "connection authority.*port",
        ),
        (
            "postgresql://operator@db.example.test/company"
            "?host=evil.example&sslmode=verify-full&sslrootcert=system",
            "connection authority.*host",
        ),
        (
            "postgresql://operator@db.example.test/company"
            "?hostaddr=127.0.0.1&sslmode=verify-full&sslrootcert=system",
            "connection authority.*hostaddr",
        ),
        (
            "postgresql://operator@db.example.test/company"
            "?service=untrusted&sslmode=verify-full&sslrootcert=system",
            "connection authority.*service",
        ),
        (
            "postgresql://operator@db.example.test/company"
            "?sslcert=/tmp/client.crt&sslkey=/tmp/client.key"
            "&sslmode=verify-full&sslrootcert=system",
            "connection authority.*sslcert.*sslkey",
        ),
    ],
)
def test_tls_database_url_fails_closed(value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        require_tls_database_url(value)
