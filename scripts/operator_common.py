"""Shared fail-closed helpers for offline operator commands."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from ai_nature_company.database_url import require_tls_database_url

MAX_IDENTIFIER_LENGTH = 256
MAX_JSON_BYTES = 1_048_576
DATABASE_URL_ENV = "ANC_OPERATOR_DATABASE_URL"
DATABASE_URL_FILE_ENV = "ANC_OPERATOR_DATABASE_URL_FILE"
OPERATOR_DATABASE_ROLE = "ai_nature_company_operator"
RUNTIME_DATABASE_ROLE = "ai_nature_company_runtime"


class OperatorError(ValueError):
    """The operator command is unsafe, invalid, or conflicts with stored state."""


class Cursor(Protocol):
    def fetchone(self) -> Sequence[Any] | None:
        """Return one database row."""

    def fetchall(self) -> Sequence[Sequence[Any]]:
        """Return every remaining row."""


class Transaction(Protocol):
    def __enter__(self) -> object:
        """Enter a database transaction."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        """Commit or roll back the transaction."""


class Connection(Protocol):
    def execute(self, query: str, params: Mapping[str, object] | None = None) -> Cursor:
        """Execute one parameterized query."""

    def transaction(self) -> Transaction:
        """Create an explicit transaction context."""


def identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise OperatorError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise OperatorError(f"{field_name} must be a non-empty canonical identifier")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise OperatorError(f"{field_name} exceeds {MAX_IDENTIFIER_LENGTH} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise OperatorError(f"{field_name} cannot contain control characters")
    return value


def canonical_uuid(value: object, field_name: str) -> str:
    canonical = identifier(value, field_name)
    try:
        parsed = UUID(canonical)
    except (AttributeError, TypeError, ValueError) as error:
        raise OperatorError(f"{field_name} must be a canonical UUID") from error
    if str(parsed) != canonical:
        raise OperatorError(f"{field_name} must be a canonical UUID")
    return canonical


def revision(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OperatorError(f"{field_name} must be a non-negative integer")
    return value


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def load_json_object(path_value: str, *, label: str) -> dict[str, object]:
    try:
        with Path(path_value).open("rb") as stream:
            encoded = stream.read(MAX_JSON_BYTES + 1)
        if len(encoded) > MAX_JSON_BYTES:
            raise OperatorError(f"{label} JSON exceeds {MAX_JSON_BYTES} bytes")

        def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, item in pairs:
                if key in result:
                    raise OperatorError(f"{label} contains a duplicate JSON key")
                result[key] = item
            return result

        value = json.loads(encoded.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OperatorError(f"could not read valid {label} JSON") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise OperatorError(f"{label} must be a JSON object")
    return value


def require_exact_fields(
    value: Mapping[str, object],
    expected: set[str],
    *,
    label: str,
) -> None:
    missing = sorted(expected - value.keys())
    unknown = sorted(value.keys() - expected)
    if missing:
        raise OperatorError(f"{label} is missing fields: {', '.join(missing)}")
    if unknown:
        raise OperatorError(f"{label} contains unknown fields: {', '.join(unknown)}")


def database_url_from_environment(environ: Mapping[str, str] | None = None) -> str:
    values = os.environ if environ is None else environ
    inline = values.get(DATABASE_URL_ENV)
    file_name = values.get(DATABASE_URL_FILE_ENV)
    if inline and file_name:
        raise OperatorError(f"set only one of {DATABASE_URL_ENV} and {DATABASE_URL_FILE_ENV}")
    if inline:
        if inline != inline.strip() or "\n" in inline or "\r" in inline or "\x00" in inline:
            raise OperatorError(f"{DATABASE_URL_ENV} must contain one canonical value")
        try:
            return require_tls_database_url(inline, field_name=DATABASE_URL_ENV)
        except ValueError as error:
            raise OperatorError(str(error)) from error
    if file_name:
        path = Path(file_name)
        if path.is_symlink():
            raise OperatorError(f"{DATABASE_URL_FILE_ENV} must not name a symlink")
        try:
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise OperatorError(f"{DATABASE_URL_FILE_ENV} must name a regular 0600 file")
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise OperatorError(
                    f"{DATABASE_URL_FILE_ENV} file must be owned by the current user"
                )
            value = path.read_text(encoding="utf-8")
        except OSError as error:
            raise OperatorError("could not read the operator database URL file") from error
        if value.endswith("\n"):
            value = value[:-1]
        if not value or value != value.strip() or "\n" in value or "\r" in value:
            raise OperatorError("operator database URL file must contain one value")
        try:
            return require_tls_database_url(value, field_name=DATABASE_URL_FILE_ENV)
        except ValueError as error:
            raise OperatorError(str(error)) from error
    raise OperatorError(
        f"set {DATABASE_URL_ENV} or {DATABASE_URL_FILE_ENV}; no database URL flag is accepted"
    )


def require_operator_role(connection: Connection) -> None:
    row = connection.execute(
        """
        SELECT session_user,
               current_user,
               role.rolsuper,
               role.rolbypassrls,
               role.rolcreatedb,
               role.rolcreaterole,
               role.rolreplication,
               role.rolinherit,
               role.rolcanlogin,
               NOT EXISTS (
                   SELECT 1
                   FROM pg_catalog.pg_auth_members AS membership
                   WHERE membership.member = role.oid
               ) AS has_no_role_memberships
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = current_user
        """
    ).fetchone()
    if row is None or len(row) != 10:
        raise OperatorError("could not verify the database operator role")
    if row[0] != OPERATOR_DATABASE_ROLE:
        raise OperatorError(f"database session_user must be {OPERATOR_DATABASE_ROLE}")
    if row[1] != OPERATOR_DATABASE_ROLE:
        raise OperatorError(f"database current_user must be {OPERATOR_DATABASE_ROLE}")
    if any(flag is not False for flag in row[2:8]) or row[8] is not True or row[9] is not True:
        raise OperatorError("the database operator role has unsafe attributes")


def connect_operator(dsn: str) -> Any:
    try:
        import psycopg
    except ImportError as error:  # pragma: no cover - packaging failure
        raise OperatorError("this command requires the project psycopg dependency") from error
    return psycopg.connect(dsn)
