from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scripts.provision_agent_binding import (
    OPERATOR_DATABASE_ROLE,
    ProvisioningError,
    ProvisioningSpec,
    apply_rotation,
    parameterized_plan,
)
from tests.operations.test_provision_agent_binding import (
    CELL_ID,
    COMPANY_ID,
    FakeConnection,
    FakeCursor,
    definition,
    manifest,
)

NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)
NEW_RUN_ID = "00000000-0000-4000-8000-0000000000aa"
OLD_RUN_ID = "00000000-0000-4000-8000-000000000007"


def rotation_manifest(**overrides: object) -> dict[str, object]:
    value = manifest(
        agent_run_id=NEW_RUN_ID,
        run_started_at="2026-08-30T11:30:00Z",
        run_expires_at="2026-09-06T11:30:00Z",
        run_created_at="2026-08-30T11:30:00Z",
        rotation_kind="expiry",
    )
    value.update(overrides)
    return value


def rotation_spec(**overrides: object) -> ProvisioningSpec:
    return ProvisioningSpec.from_mapping(
        rotation_manifest(**overrides),
        definition=definition(),
        now=NOW,
        rotate=True,
    )


def test_rotation_manifest_keeps_binding_time_separate_from_run_creation() -> None:
    desired = rotation_spec()
    assert desired.provisioned_at == datetime(2026, 8, 30, 9, tzinfo=UTC)
    assert desired.run_created_at == datetime(2026, 8, 30, 11, 30, tzinfo=UTC)
    assert desired.rotation_kind == "expiry"
    assert desired.agent_run_id == NEW_RUN_ID


def test_initial_manifest_rejects_rotation_fields() -> None:
    with pytest.raises(ProvisioningError, match="unknown fields"):
        ProvisioningSpec.from_mapping(
            rotation_manifest(),
            definition=definition(),
            now=NOW,
        )


def test_rotation_plan_retires_expired_run_before_insert() -> None:
    plan = parameterized_plan(rotation_spec(), rotate=True)
    serialized = "\n".join(str(step["sql"]) for step in plan["steps"])
    assert plan["rotation"] is True
    assert plan["session_behavior"] == "preserve_post_cutoff_session"
    assert "expire_predecessor_run" in {step["name"] for step in plan["steps"]}
    assert "UPDATE public.anc_agent_runs" in serialized
    assert "INSERT INTO public.anc_agent_runs" in serialized
    assert COMPANY_ID not in serialized


class RotationConnection(FakeConnection):
    def __init__(
        self,
        desired: ProvisioningSpec,
        *,
        running_run: tuple[object, ...] | None,
        successor_exists: bool = False,
        token_valid_after: datetime | None = None,
        subject_created_at: datetime | None = None,
        client_resource_url: str | None = None,
        alias_exists: bool = True,
        alias_status: str = "active",
    ) -> None:
        super().__init__(
            desired,
            client_resource_url=client_resource_url,
            alias_exists=alias_exists,
            alias_status=alias_status,
        )
        self.running_run = running_run
        self.successor_exists = successor_exists
        self.token_valid_after = token_valid_after or desired.provisioned_at
        self.subject_created_at = subject_created_at or desired.provisioned_at
        self.expired: tuple[object, ...] | None = None

    def execute(
        self,
        query: str,
        params: dict[str, object] | None = None,
    ) -> FakeCursor:
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
            return super().execute(query, params)
        if query.lstrip().startswith("SELECT company_id, client_id"):
            return super().execute(query, params)
        if "token_valid_after, disabled_at, created_at" in query:
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
                    self.token_valid_after,
                    None,
                    self.subject_created_at,
                )
            )
        if query.lstrip().startswith("SELECT company_id, agent_instance_id"):
            return super().execute(query, params)
        if "AND status = %(running_status)s" in query and "FOR UPDATE" in query:
            return FakeCursor(self.running_run)
        if query.lstrip().startswith("UPDATE public.anc_agent_runs"):
            self.executions.append((query, params))
            assert params is not None
            self.expired = (
                "expired",
                params["run_created_at"],
                "run_expired",
            )
            self.running_run = None
            return FakeCursor()
        if "SELECT status, ended_at, termination_code" in query:
            return FakeCursor(self.expired)
        if query.lstrip().startswith("SELECT company_id, agent_run_id"):
            if not self.successor_exists and self.expired is None:
                return FakeCursor()
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
        return super().execute(query, params)


def _expired_running_row(desired: ProvisioningSpec) -> tuple[object, ...]:
    return (
        desired.company_id,
        OLD_RUN_ID,
        desired.agent_instance_id,
        desired.principal_id,
        desired.definition_id,
        desired.definition_version,
        desired.role,
        CELL_ID,
        "running",
        datetime(2026, 8, 30, 10, tzinfo=UTC),
        NOW - timedelta(minutes=1),
        None,
        None,
        0,
    )


def test_rotate_expires_predecessor_and_inserts_one_successor() -> None:
    desired = rotation_spec()
    connection = RotationConnection(desired, running_run=_expired_running_row(desired))

    result = apply_rotation(desired, connection, now=NOW)

    assert result["agent_run_id"] == NEW_RUN_ID
    assert connection.expired == ("expired", desired.run_created_at, "run_expired")
    assert any(
        query.lstrip().startswith("UPDATE public.anc_agent_runs")
        for query, _ in connection.executions
    )
    assert any("INSERT INTO public.anc_agent_runs" in query for query, _ in connection.executions)
    assert connection.transactions[0].committed


def test_rotate_is_idempotent_when_successor_already_exists() -> None:
    desired = rotation_spec()
    connection = RotationConnection(desired, running_run=None, successor_exists=True)

    first = apply_rotation(desired, connection, now=NOW)
    second = apply_rotation(desired, connection, now=NOW)

    assert first["agent_run_id"] == second["agent_run_id"] == NEW_RUN_ID
    assert all(transaction.committed for transaction in connection.transactions)


def test_rotate_fails_on_a_non_expired_competing_run() -> None:
    desired = rotation_spec()
    live = _expired_running_row(desired)
    live_list = list(live)
    live_list[10] = NOW + timedelta(hours=1)
    connection = RotationConnection(desired, running_run=tuple(live_list))

    with pytest.raises(ProvisioningError, match="non-expired competing run"):
        apply_rotation(desired, connection, now=NOW)
    assert connection.transactions[0].rolled_back
    assert connection.expired is None


def test_revocation_replacement_refuses_to_expire_a_running_run() -> None:
    desired = rotation_spec(rotation_kind="revocation_replacement")
    connection = RotationConnection(
        desired,
        running_run=_expired_running_row(desired),
        token_valid_after=NOW,
    )

    with pytest.raises(ProvisioningError, match="revoked first"):
        apply_rotation(desired, connection, now=NOW)
    assert connection.transactions[0].rolled_back


def test_rotate_accepts_an_existing_resource_alias_of_the_same_client() -> None:
    desired = rotation_spec(resource_url="https://alias-host.example.test/mcp")
    connection = RotationConnection(
        desired,
        running_run=_expired_running_row(desired),
        client_resource_url="https://mcp.example.test/mcp",
    )

    result = apply_rotation(desired, connection, now=NOW)

    assert result["agent_run_id"] == NEW_RUN_ID
    assert not any(
        query.lstrip().startswith("INSERT INTO public.anc_oauth_client_resource_aliases")
        for query, _ in connection.executions
    )
    assert connection.transactions[0].committed


def test_rotate_rejects_a_resource_the_client_does_not_list() -> None:
    desired = rotation_spec(resource_url="https://unknown-host.example.test/mcp")
    connection = RotationConnection(
        desired,
        running_run=_expired_running_row(desired),
        client_resource_url="https://mcp.example.test/mcp",
        alias_exists=False,
    )

    with pytest.raises(ProvisioningError, match="does not list this protected resource"):
        apply_rotation(desired, connection, now=NOW)
    assert connection.transactions[0].rolled_back


def test_rotate_requires_the_operator_role() -> None:
    desired = rotation_spec()
    connection = RotationConnection(desired, running_run=_expired_running_row(desired))
    connection.role = "ai_nature_company_runtime"

    with pytest.raises(ProvisioningError, match=OPERATOR_DATABASE_ROLE):
        apply_rotation(desired, connection, now=NOW)
