"""A fresh host can resume and independently review a bet through MCP reads."""

from __future__ import annotations

import json

import pytest
from mcp.client import Client

from ai_nature_company.auth import (
    AgentIdentityContext,
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
)
from ai_nature_company.errors import AuthorizationError, ValidationError
from ai_nature_company.server import StaticAuthResolver, build_server

from .scenario import Scenario
from .support import (
    CELL_ID,
    COMPANY_ID,
    EVALUATION_ID,
    EVIDENCE_ID,
    EXPERIMENT_ID,
    PRINCIPAL_IDS,
    RUN_IDS,
    WORK_ITEM_ID,
    load_definition,
    stable_uuid,
)


def bound_principal(actor: str):
    kind = "cell-owner" if actor == "owner" else actor
    role = Role.CELL_OWNER if actor == "owner" else Role(actor)
    definition = load_definition(kind)
    record = PrincipalRecord(
        principal_id=PRINCIPAL_IDS[actor],
        grants=(ScopedRoleGrant(role, ResourceScope.company(COMPANY_ID)),),
        agent_run_id=RUN_IDS[actor],
        agent_identity=AgentIdentityContext(
            agent_instance_id=definition["definition_id"],
            definition_id=definition["definition_id"],
            definition_version=definition["version"],
            role=role,
            cell_id=CELL_ID,
        ),
    )
    return LocalTestAuthenticator({"test-session": record}).authenticate("test-session")


async def read_records(scenario: Scenario, actor: str, kind: str, **page):
    result = await scenario.service.invoke(
        "company.describe",
        bound_principal(actor),
        {"cell_records": {"cell_id": CELL_ID, "kind": kind, **page}},
    )
    return result["cell_records"]


@pytest.mark.asyncio
async def test_fresh_specialist_discovers_work_without_handoff_or_lease_disclosure() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    await scenario.create_work()
    before = await scenario.store.export_company(COMPANY_ID)

    page = await read_records(scenario, "specialist", "work_item")
    work = next(record for record in page["records"] if record["record_id"] == WORK_ITEM_ID)
    assert work["state"]["outcome_contract"]["acceptance_criteria"]
    assert work["state"]["approval_requirements"]
    assert work["revision"] == work["state"]["revision"] == 1
    assert page["next_after_id"] is None
    assert page["observed_at"] == scenario.clock().isoformat().replace("+00:00", "Z")
    after = await scenario.store.export_company(COMPANY_ID)
    assert {k: v for k, v in after.items() if k != "exported_at"} == {
        k: v for k, v in before.items() if k != "exported_at"
    }

    await scenario.claim_work()
    claimed = await read_records(scenario, "specialist", "work_item")
    assert claimed["records"][0]["state"]["status"] == "claimed"
    serialized = json.dumps(claimed)
    assert scenario.responses["work.claim"]["state"]["lease"]["token"] not in serialized
    assert "lease_token" not in serialized
    assert "work_runtime" not in serialized


@pytest.mark.asyncio
async def test_evaluator_discovers_tick_work_frozen_protocol_and_actual_evidence() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    await scenario.create_work()
    await scenario.claim_work()
    await scenario.append_evidence()
    await scenario.complete_work()
    scenario.clock.advance(days=8)
    tick = await scenario.service.tick(COMPANY_ID)

    work = await read_records(scenario, "evaluator", "work_item")
    closeouts = [r for r in work["records"] if r["state"]["kind"] == "evaluate_experiment"]
    assert [r["record_id"] for r in closeouts] == tick["closeout_work_item_ids"]
    experiments = await read_records(scenario, "evaluator", "experiment")
    frozen = next(r["state"] for r in experiments["records"] if r["record_id"] == EXPERIMENT_ID)
    assert frozen["state"] == "evaluating"
    assert frozen["hypothesis"]
    assert frozen["metrics"] and frozen["success_thresholds"] and frozen["failure_thresholds"]
    assert frozen["evaluator_principal_id"] == PRINCIPAL_IDS["evaluator"]
    assert frozen["executor_principal_ids"] == [PRINCIPAL_IDS["specialist"]]
    evidence = await read_records(scenario, "evaluator", "evidence")
    observed = next(r["state"] for r in evidence["records"] if r["record_id"] == EVIDENCE_ID)
    assert observed["source_reference"]
    assert observed == scenario.responses[f"evidence.append:{EVIDENCE_ID}"]["state"]["evidence"]
    assert observed["lineage"]["relation"] == "original"


@pytest.mark.asyncio
async def test_experiment_read_carries_linked_evaluation_without_rewriting_evidence() -> None:
    scenario = Scenario()
    await scenario.build_evaluated_experiment()
    page = await read_records(scenario, "owner", "experiment")
    record = next(r for r in page["records"] if r["record_id"] == EXPERIMENT_ID)
    assert record["evaluation"]["evaluation_id"] == EVALUATION_ID
    assert record["evaluation"]["independence_attestation"]["checked_by_substrate"] is True


@pytest.mark.asyncio
async def test_offer_read_revision_supports_mutation_from_first_and_later_versions() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    await scenario.create_cell()
    await scenario.register_offer()
    for version in (1, 2, 3):
        page = await read_records(scenario, "owner", "offer")
        parent = next(r for r in page["records"] if r["state"]["version"] == version)
        assert parent["revision"] == version - 1
        result = await scenario.invoke(
            "offer.mutate",
            "owner",
            {
                "parent_offer_version_id": parent["record_id"],
                "mutation_summary": f"Test immutable mutation from version {version}.",
                "changes": {"risk_reversal": f"Inspectable test version {version + 1}."},
            },
            expected_revision=parent["revision"],
            key=f"mutate-from-discovery-v{version}",
        )
        assert result["state"]["offer"]["version"] == version + 1
        assert result["state"]["offer"]["parent_offer_version_id"] == parent["record_id"]


@pytest.mark.asyncio
async def test_cell_records_filter_before_paging_and_preserve_revision() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    other_cell = stable_uuid("read-context-other-cell")
    async with scenario.store.transaction(COMPANY_ID) as tx:
        for i in range(61):
            await tx.put_projection(
                projection_type="evidence",
                projection_id=f"{i:08d}-0000-4000-8000-000000000000",
                state={"cell_id": other_cell, "private_other_cell_value": i},
                expected_revision=0,
            )
        for i in range(3):
            await tx.put_projection(
                projection_type="evidence",
                projection_id=f"9999999{i}-0000-4000-8000-000000000000",
                state={"cell_id": CELL_ID, "observation": {"summary": f"Observation {i}"}},
                expected_revision=0,
            )
    first = await read_records(scenario, "evaluator", "evidence", limit=2)
    second = await read_records(
        scenario, "evaluator", "evidence", limit=2, after_id=first["next_after_id"]
    )
    assert len(first["records"]) == 2
    assert len(second["records"]) == 1
    assert second["next_after_id"] is None
    assert first["next_after_id"] == first["records"][-1]["record_id"]
    assert "private_other_cell_value" not in json.dumps([first, second])
    assert all(r["revision"] == 1 for r in first["records"] + second["records"])


@pytest.mark.asyncio
async def test_cell_records_reject_cross_cell_and_unverified_authority() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    for cell_id in (stable_uuid("other-cell"), stable_uuid("nonexistent-cell")):
        with pytest.raises(AuthorizationError, match="does not cover"):
            await scenario.service.invoke(
                "company.describe",
                bound_principal("specialist"),
                {"cell_records": {"cell_id": cell_id, "kind": "work_item"}},
            )
    with pytest.raises(AuthorizationError, match="verified principal"):
        await scenario.service.invoke(
            "company.describe",
            {"role": "founder", "company_id": COMPANY_ID},
            {"cell_records": {"cell_id": CELL_ID, "kind": "evidence"}},
        )
    with pytest.raises(AuthorizationError, match="does not cover"):
        await scenario.service.invoke(
            "company.describe",
            scenario.principals["specialist"],
            {"cell_records": {"cell_id": stable_uuid("legacy-cross-cell"), "kind": "evidence"}},
        )


@pytest.mark.asyncio
async def test_read_size_limit_pages_without_truncation_and_redacts_nested_secrets() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    evidence_ids = [f"9999999{i}-0000-4000-8000-000000000000" for i in range(3)]
    async with scenario.store.transaction(COMPANY_ID) as tx:
        for evidence_id in evidence_ids:
            await tx.put_projection(
                projection_type="evidence",
                projection_id=evidence_id,
                state={
                    "cell_id": CELL_ID,
                    "observation": {"summary": "a" * 100_000, "credentials": "never-return-this"},
                },
                expected_revision=0,
            )
    first = await read_records(scenario, "evaluator", "evidence", limit=50)
    second = await read_records(
        scenario, "evaluator", "evidence", limit=50, after_id=first["next_after_id"]
    )
    assert [r["record_id"] for r in first["records"] + second["records"]] == evidence_ids
    assert len(first["records"]) == 2
    assert len(json.dumps(first).encode()) < 262_144
    assert len(first["records"][0]["state"]["observation"]["summary"]) == 100_000
    assert "never-return-this" not in json.dumps([first, second])
    async with scenario.store.transaction(COMPANY_ID) as tx:
        await tx.put_projection(
            projection_type="evidence",
            projection_id=evidence_ids[0],
            state={"cell_id": CELL_ID, "observation": {"summary": "a" * 300_000}},
            expected_revision=1,
        )
    with pytest.raises(ValidationError, match="exceeds the bounded read size"):
        await read_records(scenario, "evaluator", "evidence")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"kind": "work_runtime"},
        {"kind": "agent_binding"},
        {"limit": 0},
        {"limit": 51},
        {"limit": True},
        {"after_id": "not-a-uuid"},
        {"company_id": stable_uuid("caller-cannot-pick-company")},
    ],
)
async def test_cell_records_reject_unsupported_or_unbounded_queries(extra) -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    with pytest.raises(ValidationError):
        await scenario.service.invoke(
            "company.describe",
            bound_principal("specialist"),
            {"cell_records": {"cell_id": CELL_ID, "kind": "evidence", **extra}},
        )


@pytest.mark.asyncio
async def test_protocol_explains_host_neutral_operation_and_existing_restrictions() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    protocol = await scenario.service.invoke("protocol.describe", bound_principal("specialist"), {})
    contract = protocol["bet_workflow"]
    assert contract["version"] == 1
    assert contract["context_read"]["tool"] == "company.describe"
    assert contract["context_read"]["max_records"] == 50
    assert contract["identity_activation"]["first_call"] == "company.describe"
    assert contract["external_action_authority"] == "not_granted_by_discovery_or_work_lease"
    assert contract["evaluation"]["independent_principal_required"] is True
    assert contract["blocked_paths"] == ["cell.scale", "cell.unpause", "learning.promote"]
    assert contract["handoff"]["default"] == "write_ledger_then_next_force_reads"
    assert contract["handoff"]["next_step"] == ["stop", "read_back"]
    assert "peer_dm" in contract["handoff"]["not"]
    assert "send_to_agent" in contract["handoff"]["not"]
    assert contract["handoff"]["channel_membership_is_not"] == "work_queue"
    assert contract["handoff"]["does_not_mean"] == "silent_to_everyone"
    assert contract["handoff"]["external_entity_chat"] == (
        "normal_and_distinct_from_force_peer_silence"
    )
    assert contract["handoff"]["external_entities_are_not"] == "anc_ledger_peers"
    assert any("Do not DM a peer" in step for step in contract["operate"])
    assert any(
        "not silence toward founder" in step and "reply when they are waiting" in step
        for step in contract["operate"]
    )


@pytest.mark.asyncio
async def test_host_reads_work_and_protocol_through_the_mcp_transport() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    await scenario.create_work()
    server = build_server(scenario.service, StaticAuthResolver(bound_principal("specialist")))
    async with Client(server) as client:
        identity = await client.call_tool("company.describe", {})
        protocol = await client.call_tool("protocol.describe", {})
        work = await client.call_tool(
            "company.describe",
            {"payload": {"cell_records": {"cell_id": CELL_ID, "kind": "work_item"}}},
        )
    assert identity.is_error is False
    assert identity.structured_content["principal"]["role"] == "specialist"
    assert protocol.is_error is False
    assert protocol.structured_content["bet_workflow"]["version"] == 1
    assert work.is_error is False
    assert work.structured_content["cell_records"]["records"][0]["record_id"] == WORK_ITEM_ID
