from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

import pytest

from scripts.audit_call_cardinality import audit_call_cardinality
from scripts.audit_database_contract import RUNTIME_DATABASE_ROLE, audit_database_contract
from scripts.bootstrap_closed_beta import BootstrapManifest
from scripts.contain_identity import ContainmentSpec, apply_containment, parameterized_plan
from scripts.generate_closed_beta_two_cell_manifest import (
    generate_two_cell_manifest,
    render_two_cell_manifest,
)
from scripts.list_expiring_agent_runs import (
    list_expiring_agent_runs,
    list_expiring_agent_runs_report,
)
from scripts.operator_common import OPERATOR_DATABASE_ROLE, OperatorError

COMPANY_ID = "00000000-0000-4000-8000-000000000001"
RUN_ID = "00000000-0000-4000-8000-000000000007"
NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


class FakeCursor:
    def __init__(
        self,
        row: Sequence[Any] | None = None,
        rows: Sequence[Sequence[Any]] | None = None,
    ):
        self.row = row
        self.rows = list(rows or ((row,) if row is not None else ()))

    def fetchone(self) -> Sequence[Any] | None:
        return self.row

    def fetchall(self) -> list[Sequence[Any]]:
        return list(self.rows)


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


AUTH_SUBJECT_ID = "authsub_" + ("ab" * 32)


class FakeOperatorConnection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, Mapping[str, object] | None]] = []
        self.transactions: list[FakeTransaction] = []
        self.run_status = "running"
        self.run_revision = 0
        self.subject_status = "active"
        self.subject_revision = 0
        self.instance_status = "active"
        self.instance_revision = 0
        self.client_status = "active"
        self.client_revision = 0
        self.role = OPERATOR_DATABASE_ROLE
        self.session_user = OPERATOR_DATABASE_ROLE

    def transaction(self) -> FakeTransaction:
        transaction = FakeTransaction()
        self.transactions.append(transaction)
        return transaction

    def execute(self, query: str, params: Mapping[str, object] | None = None) -> FakeCursor:
        self.executions.append((query, params))
        if "FROM pg_catalog.pg_roles" in query:
            return FakeCursor(
                (
                    self.session_user,
                    self.role,
                    False,
                    False,
                    False,
                    False,
                    False,
                    False,
                    True,
                    True,
                )
            )
        if "SELECT current_user" in query:
            return FakeCursor((self.role,))
        if "FROM public.anc_agent_runs" in query and "SELECT agent_run_id, status" in query:
            return FakeCursor((RUN_ID, self.run_status, self.run_revision, "operator_revoked"))
        if query.lstrip().startswith("UPDATE public.anc_agent_runs"):
            self.run_status = "revoked"
            self.run_revision += 1
            return FakeCursor()
        if "SELECT status, revision, termination_code" in query:
            return FakeCursor((self.run_status, self.run_revision, "operator_revoked"))
        if "FROM public.anc_auth_subjects" in query and "SELECT auth_subject_id" in query:
            return FakeCursor(("authsub_1", self.subject_status, self.subject_revision, None))
        if query.lstrip().startswith("UPDATE public.anc_auth_subjects"):
            self.subject_status = str(params["target_status"]) if params else self.subject_status
            self.subject_revision += 1
            return FakeCursor()
        if "FROM public.anc_auth_subjects" in query and "SELECT status, revision" in query:
            return FakeCursor((self.subject_status, self.subject_revision))
        if "FROM public.anc_agent_instances" in query and "SELECT agent_instance_id" in query:
            return FakeCursor(("instance-1", self.instance_status, self.instance_revision))
        if query.lstrip().startswith("UPDATE public.anc_agent_instances"):
            self.instance_status = "suspended"
            self.instance_revision += 1
            return FakeCursor()
        if "FROM public.anc_agent_instances" in query and "SELECT status, revision" in query:
            return FakeCursor((self.instance_status, self.instance_revision))
        if "FROM public.anc_oauth_clients" in query and "SELECT client_id" in query:
            return FakeCursor(("client-1", self.client_status, self.client_revision))
        if query.lstrip().startswith("UPDATE public.anc_oauth_clients"):
            self.client_status = "disabled"
            self.client_revision += 1
            return FakeCursor()
        if "FROM public.anc_oauth_clients" in query and "SELECT status, revision" in query:
            return FakeCursor((self.client_status, self.client_revision))
        if "SELECT agent_run_id, revision" in query:
            return FakeCursor()
        return FakeCursor()


def test_containment_plan_is_parameterized_and_non_secret() -> None:
    spec = ContainmentSpec.from_mapping(
        {
            "action": "revoke_run",
            "company_id": COMPANY_ID,
            "target_id": RUN_ID,
            "expected_revision": 0,
            "termination_code": "operator_revoked",
        }
    )
    plan = parameterized_plan(spec)
    serialized = json.dumps(plan)
    assert plan["transaction"] == "required"
    assert plan["history_rewrite"] is False
    assert plan["parameters"]["company_id"] == COMPANY_ID
    assert "password" not in serialized
    assert "database_url" not in serialized


def test_revoke_run_is_exact_and_idempotent() -> None:
    spec = ContainmentSpec.from_mapping(
        {
            "action": "revoke_run",
            "company_id": COMPANY_ID,
            "target_id": RUN_ID,
            "expected_revision": 0,
            "termination_code": "operator_revoked",
        }
    )
    connection = FakeOperatorConnection()
    first = apply_containment(spec, connection, occurred_at=NOW)
    assert first["status"] == "revoked"
    assert first["affected"] == 1
    second = apply_containment(
        ContainmentSpec.from_mapping(
            {
                "action": "revoke_run",
                "company_id": COMPANY_ID,
                "target_id": RUN_ID,
                "expected_revision": 1,
                "termination_code": "operator_revoked",
            }
        ),
        connection,
        occurred_at=NOW,
    )
    assert second["idempotent"] is True
    assert all(transaction.committed for transaction in connection.transactions)


def test_revoke_run_rejects_stale_revision() -> None:
    spec = ContainmentSpec.from_mapping(
        {
            "action": "revoke_run",
            "company_id": COMPANY_ID,
            "target_id": RUN_ID,
            "expected_revision": 4,
            "termination_code": "operator_revoked",
        }
    )
    connection = FakeOperatorConnection()
    with pytest.raises(OperatorError, match="expected running revision"):
        apply_containment(spec, connection, occurred_at=NOW)
    assert connection.transactions[0].rolled_back


def test_disable_subject_and_oauth_client_and_instance() -> None:
    connection = FakeOperatorConnection()
    subject = apply_containment(
        ContainmentSpec.from_mapping(
            {
                "action": "disable_subject",
                "company_id": COMPANY_ID,
                "target_id": AUTH_SUBJECT_ID,
                "expected_revision": 0,
            }
        ),
        connection,
        occurred_at=NOW,
    )
    client = apply_containment(
        ContainmentSpec.from_mapping(
            {
                "action": "disable_oauth_client",
                "company_id": COMPANY_ID,
                "target_id": "00000000-0000-4000-8000-000000000002",
                "expected_revision": 0,
            }
        ),
        connection,
        occurred_at=NOW,
    )
    instance = apply_containment(
        ContainmentSpec.from_mapping(
            {
                "action": "suspend_instance",
                "company_id": COMPANY_ID,
                "target_id": "00000000-0000-4000-8000-000000000005",
                "expected_revision": 0,
            }
        ),
        connection,
        occurred_at=NOW,
    )
    assert subject["status"] == "disabled"
    assert client["status"] == "disabled"
    assert instance["status"] == "suspended"


def test_subject_containment_accepts_derived_auth_subject_ids() -> None:
    spec = ContainmentSpec.from_mapping(
        {
            "action": "revoke_subject",
            "company_id": COMPANY_ID,
            "target_id": AUTH_SUBJECT_ID,
            "expected_revision": 0,
        }
    )
    assert spec.target_id == AUTH_SUBJECT_ID
    with pytest.raises(OperatorError, match="canonical identifier"):
        ContainmentSpec.from_mapping(
            {
                "action": "disable_subject",
                "company_id": COMPANY_ID,
                "target_id": " authsub_not-canonical",
                "expected_revision": 0,
            }
        )


def test_containment_requires_operator_role() -> None:
    spec = ContainmentSpec.from_mapping(
        {
            "action": "revoke_run",
            "company_id": COMPANY_ID,
            "target_id": RUN_ID,
            "expected_revision": 0,
        }
    )
    connection = FakeOperatorConnection()
    connection.role = RUNTIME_DATABASE_ROLE
    with pytest.raises(OperatorError, match=OPERATOR_DATABASE_ROLE):
        apply_containment(spec, connection, occurred_at=NOW)


def test_containment_rejects_set_role_without_operator_login() -> None:
    spec = ContainmentSpec.from_mapping(
        {
            "action": "revoke_run",
            "company_id": COMPANY_ID,
            "target_id": RUN_ID,
            "expected_revision": 0,
        }
    )
    connection = FakeOperatorConnection()
    connection.session_user = "postgres"
    connection.role = OPERATOR_DATABASE_ROLE
    with pytest.raises(OperatorError, match="session_user"):
        apply_containment(spec, connection, occurred_at=NOW)


def test_pause_cell_requires_operator_login_before_set_role() -> None:
    spec = ContainmentSpec.from_mapping(
        {
            "action": "pause_cell",
            "company_id": COMPANY_ID,
            "target_id": "00000000-0000-4000-8000-00000000000c",
            "expected_revision": 0,
        }
    )
    connection = FakeOperatorConnection()
    connection.session_user = "postgres"
    connection.role = OPERATOR_DATABASE_ROLE
    with pytest.raises(OperatorError, match="session_user"):
        apply_containment(
            spec,
            connection,
            occurred_at=NOW,
            database_url="postgresql://postgres@db.example.test:5432/anc",
        )


def test_two_cell_example_is_deterministic_and_bootstrap_valid() -> None:
    first = generate_two_cell_manifest()
    second = generate_two_cell_manifest()
    assert dict(first) == dict(second)
    serialized = render_two_cell_manifest()
    assert "supabase" not in serialized.casefold()
    assert "password" not in serialized.casefold()
    assert "access_token" not in serialized.casefold()
    parsed = BootstrapManifest.from_mapping(json.loads(serialized))
    roles = [agent.role.value for agent in parsed.agents]
    cells = [cell.cell_id for cell in parsed.cells]
    assert roles.count("cell_owner") == 2
    assert "specialist" in roles
    assert "evaluator" in roles
    assert len(cells) == 2
    owners = [agent for agent in parsed.agents if agent.role.value == "cell_owner"]
    assert {owner.cell_id for owner in owners} == set(cells)


class ContractConnection(FakeOperatorConnection):
    def execute(self, query: str, params: Mapping[str, object] | None = None) -> FakeCursor:
        self.executions.append((query, params))
        if "SELECT current_user" in query:
            return FakeCursor((RUNTIME_DATABASE_ROLE,))
        if "FROM pg_catalog.pg_roles" in query:
            return FakeCursor((True, False, False, False, False, False, False, False))
        if "has_schema_privilege" in query:
            return FakeCursor((True, False))
        if "relforcerowsecurity" in query:
            tables = [
                "anc_agent_instances",
                "anc_agent_runs",
                "anc_audit_events",
                "anc_auth_subjects",
                "anc_budget_reservations",
                "anc_budgets",
                "anc_domain_events",
                "anc_idempotency_records",
                "anc_mcp_tool_calls",
                "anc_oauth_client_resource_aliases",
                "anc_oauth_clients",
                "anc_projections",
                "anc_stream_heads",
                "anc_work_leases",
            ]
            return FakeCursor(rows=[(table, True, True) for table in tables])
        if "has_table_privilege" in query:
            from scripts.audit_database_contract import (
                GOVERNED_TABLES,
                RUNTIME_TABLE_PRIVILEGES,
                TABLE_PRIVILEGES,
            )

            rows = []
            for table in GOVERNED_TABLES:
                for privilege in TABLE_PRIVILEGES:
                    rows.append((table, privilege, privilege in RUNTIME_TABLE_PRIVILEGES[table]))
            return FakeCursor(rows=rows)
        if "has_sequence_privilege" in query:
            from scripts.audit_database_contract import (
                RUNTIME_SEQUENCE_PRIVILEGES,
                SEQUENCE_PRIVILEGES,
                SEQUENCES,
            )

            rows = []
            for sequence in SEQUENCES:
                for privilege in SEQUENCE_PRIVILEGES:
                    rows.append(
                        (sequence, privilege, privilege in RUNTIME_SEQUENCE_PRIVILEGES[sequence])
                    )
            return FakeCursor(rows=rows)
        if "required.signature" in query:
            return FakeCursor(
                rows=[
                    ("anc_resolve_agent_identity(text,text,text,text,text)", True, True),
                    ("anc_inspect_agent_run_binding(text,text,text,text,text)", True, True),
                    ("anc_auth_catalog_compatible()", True, True),
                    ("anc_list_expiring_agent_runs()", True, True),
                ]
            )
        if "anc_validate_mcp_tool_call_attribution" in query:
            return FakeCursor((True, False))
        if "required.trigger_name" in query:
            from scripts.audit_database_contract import REQUIRED_TRIGGERS

            return FakeCursor(rows=[tuple(item) for item in REQUIRED_TRIGGERS])
        if "required.function_name" in query:
            return FakeCursor(rows=[])
        return FakeCursor()


def test_list_expiring_agent_runs_report_is_content_minimized() -> None:
    expires_at = datetime(2026, 10, 3, 12, tzinfo=UTC)
    report = list_expiring_agent_runs_report(
        (("run-soon", "instance-1", expires_at),),
        company_id=COMPANY_ID,
        observed_at=NOW,
    )
    assert report["count"] == 1
    assert report["window"] == "72h"
    assert report["company_id"] == COMPANY_ID
    assert report["runs"] == [
        {
            "agent_run_id": "run-soon",
            "agent_instance_id": "instance-1",
            "expires_at": "2026-10-03T12:00:00Z",
        }
    ]
    encoded = json.dumps(report)
    assert "subject" not in encoded
    assert "issuer" not in encoded


def test_list_expiring_agent_runs_sets_company_scope_in_a_transaction() -> None:
    connection = FakeOperatorConnection()
    report = list_expiring_agent_runs(
        connection,
        company_id=COMPANY_ID,
        observed_at=NOW,
    )
    assert report["status"] == "ok"
    assert report["company_id"] == COMPANY_ID
    assert len(connection.transactions) == 1
    assert connection.transactions[0].committed is True
    assert connection.executions[1] == (
        "SELECT set_config('app.company_id', %(company_id)s, true)",
        {"company_id": COMPANY_ID},
    )
    assert any("FROM public.anc_agent_runs" in query for query, _ in connection.executions)


def test_database_contract_audit_passes_the_runtime_contract() -> None:
    report = audit_database_contract(ContractConnection(), role_name=RUNTIME_DATABASE_ROLE)
    assert report["status"] == "pass"
    assert report["attribution_public_execute"] is False
    assert report["public_schema_create"] is False
    assert report["required_triggers"] is True


def test_database_contract_audit_fails_when_required_triggers_are_missing() -> None:
    class MissingTriggerConnection(ContractConnection):
        def execute(self, query: str, params: Mapping[str, object] | None = None) -> FakeCursor:
            if "required.trigger_name" in query:
                return FakeCursor(rows=[])
            return super().execute(query, params)

    with pytest.raises(OperatorError, match="required trigger"):
        audit_database_contract(MissingTriggerConnection(), role_name=RUNTIME_DATABASE_ROLE)


def test_cardinality_audit_fails_closed_on_zero_or_multiple_terminals() -> None:
    class CardinalityConnection(FakeOperatorConnection):
        def execute(self, query: str, params: Mapping[str, object] | None = None) -> FakeCursor:
            if "FROM pg_catalog.pg_roles" in query:
                return FakeCursor(
                    (
                        OPERATOR_DATABASE_ROLE,
                        OPERATOR_DATABASE_ROLE,
                        False,
                        False,
                        False,
                        False,
                        False,
                        False,
                        True,
                        True,
                    )
                )
            if "COUNT(terminal.call_id)" in query:
                return FakeCursor(rows=[("call-1", "sub", "prin", "inst", "run", 0)])
            return FakeCursor(rows=[])

    with pytest.raises(OperatorError, match="exactly one terminal"):
        audit_call_cardinality(CardinalityConnection(), company_id=COMPANY_ID)
