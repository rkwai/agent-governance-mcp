from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from scripts.provision_agent_binding import (
    DATABASE_URL_ENV,
    DATABASE_URL_FILE_ENV,
    MAX_JSON_BYTES,
    OPERATOR_DATABASE_ROLE,
    RUNTIME_DATABASE_ROLE,
    ProvisioningError,
    ProvisioningSpec,
    apply_provisioning,
    database_url_from_environment,
    load_spec,
    main,
    parameterized_plan,
)

COMPANY_ID = "00000000-0000-4000-8000-000000000001"
CLIENT_ID = "00000000-0000-4000-8000-000000000002"
SUBJECT_ID = "00000000-0000-4000-8000-000000000003"
PRINCIPAL_ID = "00000000-0000-4000-8000-000000000004"
INSTANCE_ID = "00000000-0000-4000-8000-000000000005"
DEFINITION_ID = "10000000-0000-4000-8000-000000000002"
IMMUNE_DEFINITION_ID = "10000000-0000-4000-8000-000000000005"
CELL_ID = "00000000-0000-4000-8000-000000000006"
RUN_ID = "00000000-0000-4000-8000-000000000007"
NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)
STARTED = datetime(2026, 8, 30, 10, tzinfo=UTC)
EXPIRES = datetime(2026, 9, 1, 10, tzinfo=UTC)


def manifest(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "company_id": COMPANY_ID,
        "oauth_client_id": CLIENT_ID,
        "oauth_client_display_name": "Grok specialist / cell alpha",
        "resource_url": "https://mcp.example.test/mcp",
        "issuer": "https://project.supabase.co/auth/v1",
        "external_subject": SUBJECT_ID,
        "principal_id": PRINCIPAL_ID,
        "agent_instance_id": INSTANCE_ID,
        "definition_id": DEFINITION_ID,
        "definition_version": 1,
        "role": "specialist",
        "cell_id": CELL_ID,
        "agent_run_id": RUN_ID,
        "provisioned_at": "2026-08-30T09:00:00Z",
        "run_started_at": "2026-08-30T10:00:00Z",
        "run_expires_at": "2026-09-01T10:00:00Z",
    }
    value.update(overrides)
    return value


def definition(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "definition_id": DEFINITION_ID,
        "version": 1,
        "compatible_roles": ["specialist"],
    }
    value.update(overrides)
    return value


def spec(**overrides: object) -> ProvisioningSpec:
    return ProvisioningSpec.from_mapping(
        manifest(**overrides),
        definition=definition(),
        now=NOW,
    )


def test_manifest_creates_deterministic_non_secret_subject_binding() -> None:
    first = spec()
    second = spec()

    assert first == second
    assert first.auth_subject_id.startswith("authsub_")
    assert len(first.auth_subject_id) == len("authsub_") + 64
    assert first.role == "specialist"
    assert first.cell_id == CELL_ID


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"resource_url": "http://mcp.example.test/mcp"}, "canonical HTTPS"),
        ({"resource_url": "https://mcp.example.test/"}, "must end with /mcp"),
        ({"resource_url": "https://mcp.example.test/mcp/"}, "canonical MCP path"),
        ({"resource_url": "https://mcp.example.test/mcp/../etc"}, "canonical MCP path"),
        ({"resource_url": "https://mcp.example.test/mcp/Foo"}, "canonical MCP path"),
        ({"external_subject": "not-a-uuid"}, "canonical UUID"),
        ({"role": "founder"}, "not an agent role"),
        ({"cell_id": None}, "cell_id is required for the specialist"),
        ({"run_expires_at": "2026-08-30T11:00:00Z"}, "later than now"),
        ({"run_started_at": "2026-08-30T13:00:00Z"}, "cannot be in the future"),
        ({"client_secret": "must-not-be-here"}, "unknown fields"),
    ],
)
def test_manifest_fails_closed_on_invalid_or_secret_shaped_input(
    override: Mapping[str, object],
    message: str,
) -> None:
    with pytest.raises(ProvisioningError, match=message):
        ProvisioningSpec.from_mapping(
            manifest(**override),
            definition=definition(),
            now=NOW,
        )


def test_reviewed_definition_must_match_role_id_and_version() -> None:
    with pytest.raises(ProvisioningError, match="incompatible"):
        ProvisioningSpec.from_mapping(
            manifest(),
            definition=definition(compatible_roles=["evaluator"]),
            now=NOW,
        )
    with pytest.raises(ProvisioningError, match="definition_id"):
        ProvisioningSpec.from_mapping(
            manifest(),
            definition=definition(definition_id="10000000-0000-4000-8000-000000000003"),
            now=NOW,
        )


def test_company_wide_beta_roles_may_have_no_cell_binding() -> None:
    desired = ProvisioningSpec.from_mapping(
        manifest(role="immune", cell_id=None, definition_id=IMMUNE_DEFINITION_ID),
        definition=definition(
            definition_id=IMMUNE_DEFINITION_ID,
            compatible_roles=["immune"],
        ),
        now=NOW,
    )

    assert desired.role == "immune"
    assert desired.cell_id is None


def test_plan_separates_sql_from_values_and_never_contains_a_database_secret() -> None:
    desired = spec()
    plan = parameterized_plan(desired)
    serialized = json.dumps(plan, default=str)

    assert "database_url" not in serialized.lower()
    assert "client_secret" not in serialized.lower()
    assert "access_token" not in serialized.lower()
    steps = plan["steps"]
    assert isinstance(steps, list)
    insert_steps = [step for step in steps if str(step["name"]).startswith("insert_")]
    assert insert_steps
    for step in insert_steps:
        sql = str(step["sql"])
        assert "%(" in sql
        assert COMPANY_ID not in sql
        assert SUBJECT_ID not in sql
        assert RUN_ID not in sql
        assert step["parameters"]


def test_provisioning_sql_matches_the_identity_migration_contract() -> None:
    migration = Path(
        "supabase/migrations/202608300003_agent_identity_and_attribution.sql"
    ).read_text(encoding="utf-8")
    plan = parameterized_plan(spec())
    serialized_sql = "\n".join(str(step["sql"]) for step in plan["steps"])

    alias_migration = Path(
        "supabase/migrations/202608310007_oauth_client_resource_aliases.sql"
    ).read_text(encoding="utf-8")
    for table in (
        "anc_oauth_clients",
        "anc_auth_subjects",
        "anc_agent_instances",
        "anc_agent_runs",
    ):
        assert f"CREATE TABLE IF NOT EXISTS public.{table}" in migration
        assert f"INSERT INTO public.{table}" in serialized_sql
    assert (
        "CREATE TABLE IF NOT EXISTS public.anc_oauth_client_resource_aliases"
    ) in alias_migration
    assert "INSERT INTO public.anc_oauth_client_resource_aliases" in serialized_sql
    for projection_type in ("company", "cell", "agent_definition", "agent", "agent_binding"):
        assert f"projection_type = '{projection_type}'" in serialized_sql
    assert "subject_kind IN ('agent', 'human', 'service')" in migration
    assert "status IN ('running', 'succeeded', 'failed', 'cancelled', 'revoked', 'expired')" in (
        migration
    )
    assert "ON CONFLICT DO NOTHING" in serialized_sql
    assert "UPDATE public.anc_" not in serialized_sql
    assert "%(active_status)s, %(provisioned_at)s, NULL" in serialized_sql
    assert "token_valid_after, disabled_at" in serialized_sql

    operator_migration = Path("supabase/migrations/202608300005_operator_role.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE ROLE ai_nature_company_operator" in operator_migration
    assert "LOGIN" in operator_migration
    assert "NOSUPERUSER" in operator_migration
    assert "NOBYPASSRLS" in operator_migration
    assert "NOREPLICATION" in operator_migration
    assert "pg_catalog.pg_auth_members" in operator_migration
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE" in operator_migration
    assert "public.anc_auth_subjects" in operator_migration
    assert "public.anc_agent_instances" in operator_migration
    assert "public.anc_agent_runs" in operator_migration
    assert "GRANT DELETE" not in operator_migration
    assert "GRANT TRUNCATE" not in operator_migration


class FakeCursor:
    def __init__(self, row: Sequence[Any] | None = None) -> None:
        self.row = row

    def fetchone(self) -> Sequence[Any] | None:
        return self.row


class FakeTransaction(AbstractContextManager[object]):
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> object:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_value, traceback
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None


class FakeConnection:
    def __init__(
        self,
        desired: ProvisioningSpec,
        *,
        role: str = OPERATOR_DATABASE_ROLE,
        role_flags: tuple[bool, bool, bool, bool, bool, bool] = (
            False,
            False,
            False,
            False,
            False,
            False,
        ),
        has_no_role_memberships: bool = True,
        instance_role: str | None = None,
        client_resource_url: str | None = None,
        alias_status: str = "active",
        alias_exists: bool = True,
    ) -> None:
        self.desired = desired
        self.role = role
        self.role_flags = role_flags
        self.has_no_role_memberships = has_no_role_memberships
        self.instance_role = instance_role or desired.role
        self.client_resource_url = client_resource_url or desired.resource_url
        self.alias_status = alias_status
        self.alias_exists = alias_exists
        self.executions: list[tuple[str, Mapping[str, object] | None]] = []
        self.transactions: list[FakeTransaction] = []

    def transaction(self) -> FakeTransaction:
        transaction = FakeTransaction()
        self.transactions.append(transaction)
        return transaction

    def execute(
        self,
        query: str,
        params: Mapping[str, object] | None = None,
    ) -> FakeCursor:
        self.executions.append((query, params))
        if "FROM pg_catalog.pg_roles" in query:
            return FakeCursor((self.role, *self.role_flags, True, self.has_no_role_memberships))
        if "to_regclass('public.anc_oauth_clients')" in query:
            return FakeCursor(
                (
                    "anc_oauth_clients",
                    "anc_auth_subjects",
                    "anc_agent_instances",
                    "anc_agent_runs",
                    "anc_oauth_client_resource_aliases",
                )
            )
        if "projection_type = 'agent_definition'" in query:
            return FakeCursor(
                (
                    {
                        "definition_id": self.desired.definition_id,
                        "version": self.desired.definition_version,
                        "compatible_roles": [self.desired.role],
                    },
                    {
                        "agent_instance_id": self.desired.agent_instance_id,
                        "principal_id": self.desired.principal_id,
                        "definition_id": self.desired.definition_id,
                        "definition_version": self.desired.definition_version,
                        "role": self.desired.role,
                        "cell_id": self.desired.cell_id,
                        "status": "active",
                    },
                    {
                        "principal_id": self.desired.principal_id,
                        "agent_instance_id": self.desired.agent_instance_id,
                        "definition_id": self.desired.definition_id,
                        "definition_version": self.desired.definition_version,
                        "role": self.desired.role,
                        "cell_id": self.desired.cell_id,
                        "status": "active",
                    },
                    True,
                    True,
                )
            )
        if "FROM public.anc_oauth_client_resource_aliases" in query:
            if not self.alias_exists:
                return FakeCursor()
            return FakeCursor(
                (
                    self.desired.company_id,
                    self.desired.oauth_client_id,
                    self.desired.resource_url,
                    self.alias_status,
                )
            )
        if query.lstrip().startswith("SELECT company_id, client_id"):
            return FakeCursor(
                (
                    self.desired.company_id,
                    self.desired.oauth_client_id,
                    self.client_resource_url,
                    self.desired.oauth_client_display_name,
                    "active",
                )
            )
        if query.lstrip().startswith("SELECT company_id, auth_subject_id"):
            return FakeCursor(
                (
                    self.desired.company_id,
                    self.desired.auth_subject_id,
                    self.desired.issuer,
                    self.desired.external_subject,
                    self.desired.oauth_client_id,
                    self.desired.principal_id,
                    "agent",
                    "active",
                    self.desired.provisioned_at,
                    None,
                )
            )
        if query.lstrip().startswith("SELECT company_id, agent_instance_id"):
            return FakeCursor(
                (
                    self.desired.company_id,
                    self.desired.agent_instance_id,
                    self.desired.principal_id,
                    self.desired.definition_id,
                    self.desired.definition_version,
                    self.instance_role,
                    self.desired.cell_id,
                    "active",
                )
            )
        if query.lstrip().startswith("SELECT company_id, agent_run_id"):
            return FakeCursor(
                (
                    self.desired.company_id,
                    self.desired.agent_run_id,
                    self.desired.agent_instance_id,
                    self.desired.principal_id,
                    self.desired.definition_id,
                    self.desired.definition_version,
                    self.desired.role,
                    self.desired.cell_id,
                    "running",
                    self.desired.run_started_at,
                    self.desired.run_expires_at,
                    None,
                    None,
                )
            )
        return FakeCursor()


def test_apply_uses_one_scoped_transaction_and_is_exact_idempotent() -> None:
    desired = spec()
    connection = FakeConnection(desired)

    assert apply_provisioning(desired, connection)["agent_instance_id"] == INSTANCE_ID
    assert apply_provisioning(desired, connection)["agent_instance_id"] == INSTANCE_ID
    assert len(connection.transactions) == 2
    assert all(transaction.committed for transaction in connection.transactions)
    assert any("pg_advisory_xact_lock" in query for query, _ in connection.executions)
    assert any("set_config('app.company_id'" in query for query, _ in connection.executions)
    for query, params in connection.executions:
        if params is not None:
            assert COMPANY_ID not in query
            assert params["company_id"] == COMPANY_ID


def test_apply_rolls_back_when_existing_immutable_binding_differs() -> None:
    desired = spec()
    connection = FakeConnection(desired, instance_role="evaluator")

    with pytest.raises(ProvisioningError, match="agent instance differs"):
        apply_provisioning(desired, connection)
    assert connection.transactions[0].rolled_back
    assert not any(
        query.lstrip().startswith("INSERT INTO public.anc_agent_runs")
        for query, _ in connection.executions
    )


def test_apply_requires_matching_founder_registered_domain_authority() -> None:
    desired = spec()
    connection = FakeConnection(desired)
    original_execute = connection.execute

    def execute_with_missing_binding(
        query: str,
        params: Mapping[str, object] | None = None,
    ) -> FakeCursor:
        if "projection_type = 'agent_definition'" in query:
            return FakeCursor((None, None, None, True, True))
        return original_execute(query, params)

    connection.execute = execute_with_missing_binding  # type: ignore[method-assign]
    with pytest.raises(ProvisioningError, match="agent definition projection is missing"):
        apply_provisioning(desired, connection)
    assert connection.transactions[0].rolled_back
    assert not any(
        query.lstrip().startswith("INSERT INTO public.anc_oauth_clients")
        for query, _ in connection.executions
    )


def test_manifest_accepts_a_same_origin_path_alias_resource_url() -> None:
    desired = spec(resource_url="https://mcp.example.test/mcp/operating-specialist-1")

    assert desired.resource_url == "https://mcp.example.test/mcp/operating-specialist-1"


def test_apply_attaches_an_additional_resource_alias_for_the_same_client() -> None:
    desired = spec(resource_url="https://alias-host.example.test/mcp")
    connection = FakeConnection(
        desired,
        client_resource_url="https://mcp.example.test/mcp",
    )

    assert apply_provisioning(desired, connection)["oauth_client_id"] == CLIENT_ID
    assert any(
        query.lstrip().startswith("INSERT INTO public.anc_oauth_client_resource_aliases")
        for query, _ in connection.executions
    )
    assert connection.transactions[0].committed


def test_apply_attaches_a_path_resource_alias_for_the_same_client() -> None:
    desired = spec(resource_url="https://mcp.example.test/mcp/operating-specialist-1")
    connection = FakeConnection(
        desired,
        client_resource_url="https://mcp.example.test/mcp",
    )

    assert apply_provisioning(desired, connection)["oauth_client_id"] == CLIENT_ID
    assert any(
        query.lstrip().startswith("INSERT INTO public.anc_oauth_client_resource_aliases")
        for query, _ in connection.executions
    )
    assert connection.transactions[0].committed


def test_apply_rejects_a_disabled_or_missing_resource_alias() -> None:
    desired = spec(resource_url="https://alias-host.example.test/mcp")
    disabled = FakeConnection(
        desired,
        client_resource_url="https://mcp.example.test/mcp",
        alias_status="disabled",
    )
    with pytest.raises(ProvisioningError, match="OAuth client resource differs"):
        apply_provisioning(desired, disabled)
    assert disabled.transactions[0].rolled_back

    missing = FakeConnection(
        desired,
        client_resource_url="https://mcp.example.test/mcp",
        alias_exists=False,
    )
    with pytest.raises(ProvisioningError, match="OAuth client resource was not created"):
        apply_provisioning(desired, missing)
    assert missing.transactions[0].rolled_back


def test_apply_requires_the_exact_offline_operator_database_role() -> None:
    desired = spec()
    connection = FakeConnection(desired, role=RUNTIME_DATABASE_ROLE)

    with pytest.raises(ProvisioningError, match=OPERATOR_DATABASE_ROLE):
        apply_provisioning(desired, connection)
    assert connection.transactions[0].rolled_back


def test_apply_refuses_an_operator_role_with_unsafe_attributes() -> None:
    desired = spec()
    connection = FakeConnection(
        desired,
        role_flags=(False, False, False, False, True, False),
    )

    with pytest.raises(ProvisioningError, match="unsafe attributes"):
        apply_provisioning(desired, connection)
    assert connection.transactions[0].rolled_back

    membership_connection = FakeConnection(desired, has_no_role_memberships=False)
    with pytest.raises(ProvisioningError, match="unsafe attributes"):
        apply_provisioning(desired, membership_connection)
    assert membership_connection.transactions[0].rolled_back


def test_database_url_is_only_loaded_from_environment_or_owner_only_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = (
        "postgresql://operator:secret@db.example.test/postgres"
        "?sslmode=verify-full&sslrootcert=system"
    )
    assert database_url_from_environment({DATABASE_URL_ENV: dsn}) == dsn

    secret_file = tmp_path / "database-url"
    secret_file.write_text(f"{dsn}\n", encoding="utf-8")
    os.chmod(secret_file, 0o600)
    assert database_url_from_environment({DATABASE_URL_FILE_ENV: str(secret_file)}) == dsn

    os.chmod(secret_file, 0o644)
    with pytest.raises(ProvisioningError, match="regular 0600 file"):
        database_url_from_environment({DATABASE_URL_FILE_ENV: str(secret_file)})
    os.chmod(secret_file, 0o600)
    symlink = tmp_path / "database-url-link"
    symlink.symlink_to(secret_file)
    with pytest.raises(ProvisioningError, match="must not name a symlink"):
        database_url_from_environment({DATABASE_URL_FILE_ENV: str(symlink)})
    directory = tmp_path / "database-url-directory"
    directory.mkdir(mode=0o700)
    with pytest.raises(ProvisioningError, match="regular 0600 file"):
        database_url_from_environment({DATABASE_URL_FILE_ENV: str(directory)})
    with monkeypatch.context() as patch:
        owner_id = secret_file.stat().st_uid
        patch.setattr(os, "getuid", lambda: owner_id + 1)
        with pytest.raises(ProvisioningError, match="owned by the current user"):
            database_url_from_environment({DATABASE_URL_FILE_ENV: str(secret_file)})
    with pytest.raises(ProvisioningError, match="set only one"):
        database_url_from_environment(
            {DATABASE_URL_ENV: dsn, DATABASE_URL_FILE_ENV: str(secret_file)}
        )
    with pytest.raises(ProvisioningError, match="no database URL flag"):
        database_url_from_environment({})


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://operator:secret@db.example.test/postgres",
        (
            "postgresql://operator:secret@db.example.test:6543/postgres"
            "?sslmode=verify-full&sslrootcert=system"
        ),
        (
            "postgresql://operator:secret@db.example.test/postgres"
            "?sslmode=verify-full&sslrootcert=relative.crt"
        ),
    ],
)
def test_database_url_rejects_insecure_operator_connections(dsn: str) -> None:
    with pytest.raises(ProvisioningError):
        database_url_from_environment({DATABASE_URL_ENV: dsn})


def test_json_loader_rejects_duplicate_keys_without_echoing_content(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    definition_path = tmp_path / "definition.json"
    manifest_path.write_text(
        '{"company_id":"do-not-echo","company_id":"do-not-echo"}',
        encoding="utf-8",
    )
    definition_path.write_text(json.dumps(definition()), encoding="utf-8")

    with pytest.raises(ProvisioningError, match="duplicate JSON key") as captured:
        load_spec(str(manifest_path), str(definition_path), now=NOW)
    assert "do-not-echo" not in str(captured.value)


def test_json_loader_bounds_files_and_standard_input_without_echoing_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized_path = tmp_path / "oversized.json"
    oversized_path.write_bytes(b"x" * (MAX_JSON_BYTES + 1))
    definition_path = tmp_path / "definition.json"
    definition_path.write_text(json.dumps(definition()), encoding="utf-8")

    with pytest.raises(ProvisioningError, match=str(MAX_JSON_BYTES)):
        load_spec(str(oversized_path), str(definition_path), now=NOW)

    monkeypatch.setattr(sys, "stdin", StringIO("x" * (MAX_JSON_BYTES + 1)))
    with pytest.raises(ProvisioningError, match=str(MAX_JSON_BYTES)):
        load_spec("-", str(definition_path), now=NOW)


def test_cli_content_minimizes_database_errors_and_never_prints_the_dsn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import psycopg

    current = datetime.now(UTC)
    values = manifest(
        provisioned_at=(current - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"),
        run_started_at=(current - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        run_expires_at=(current + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
    )
    manifest_path = tmp_path / "manifest.json"
    definition_path = tmp_path / "definition.json"
    manifest_path.write_text(json.dumps(values), encoding="utf-8")
    definition_path.write_text(json.dumps(definition()), encoding="utf-8")
    dsn = (
        "postgresql://operator:do-not-print@db.example.test/postgres"
        "?sslmode=verify-full&sslrootcert=system"
    )
    monkeypatch.setenv(DATABASE_URL_ENV, dsn)
    monkeypatch.delenv(DATABASE_URL_FILE_ENV, raising=False)

    def fail_connect(_dsn: str) -> None:
        raise psycopg.OperationalError(f"could not connect with {_dsn}")

    monkeypatch.setattr(psycopg, "connect", fail_connect)
    result = main(
        (
            "apply",
            "--manifest",
            str(manifest_path),
            "--definition",
            str(definition_path),
        )
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "transaction was rolled back" in captured.err
    assert dsn not in captured.err
    assert "do-not-print" not in captured.err


def test_domain_authority_check_refuses_a_killed_cell() -> None:
    desired = spec()
    connection = FakeConnection(desired)
    original_execute = connection.execute
    seen: list[str] = []

    def execute_with_killed_cell(
        query: str,
        params: Mapping[str, object] | None = None,
    ) -> FakeCursor:
        if "projection_type = 'agent_definition'" in query:
            seen.append(query)
            return FakeCursor(("d", "i", "b", True, False))
        return original_execute(query, params)

    connection.execute = execute_with_killed_cell  # type: ignore[method-assign]
    with pytest.raises(ProvisioningError, match="not be killed"):
        apply_provisioning(desired, connection)
    assert "<> 'killed'" in seen[0]
    assert connection.transactions[0].rolled_back
