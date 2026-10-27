from __future__ import annotations

import pytest

from ai_nature_company.auth import (
    AgentIdentityContext,
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
)
from ai_nature_company.errors import AuthorizationError
from tests.acceptance.scenario import Scenario
from tests.acceptance.support import (
    CELL_ID,
    COMPANY_ID,
    load_definition,
    mutation,
    stable_uuid,
)

SCOPED_READER_PRINCIPAL = stable_uuid("agent-get-scoped-reader-principal")
SCOPED_READER_INSTANCE = stable_uuid("agent-get-scoped-reader-instance")
SCOPED_READER_RUN = stable_uuid("agent-get-scoped-reader-run")


def _scoped_reader_definition() -> dict[str, object]:
    definition = dict(load_definition("cell-owner"))
    definition["definition_id"] = stable_uuid("agent-get-scoped-reader-definition")
    definition["name"] = "test-scoped-agent-reader"
    definition["declared_capabilities"] = ["agent.get", "agent.list", "company.describe"]
    definition["compatible_roles"] = ["observer"]
    return definition


def _cell_bound_reader() -> object:
    definition = _scoped_reader_definition()
    record = PrincipalRecord(
        principal_id=SCOPED_READER_PRINCIPAL,
        grants=(ScopedRoleGrant(Role.OBSERVER, ResourceScope.company(COMPANY_ID)),),
        agent_run_id=SCOPED_READER_RUN,
        agent_identity=AgentIdentityContext(
            agent_instance_id=SCOPED_READER_INSTANCE,
            definition_id=str(definition["definition_id"]),
            definition_version=int(definition["version"]),
            role=Role.OBSERVER,
            cell_id=CELL_ID,
        ),
    )
    return LocalTestAuthenticator({"cell-bound-reader": record}).authenticate("cell-bound-reader")


async def _register_scoped_reader(scenario: Scenario) -> None:
    await scenario.service.invoke(
        "agent.register",
        scenario.principals["founder"],
        mutation(
            "agent.register",
            {
                "agent_instance_id": SCOPED_READER_INSTANCE,
                "principal_id": SCOPED_READER_PRINCIPAL,
                "role": "observer",
                "cell_id": CELL_ID,
                "definition": _scoped_reader_definition(),
            },
            expected_revision=0,
            key="register-agent-get-scoped-reader",
        ),
    )


@pytest.mark.asyncio
async def test_agent_get_enforces_the_same_cell_boundary_as_agent_list() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    await _register_scoped_reader(scenario)
    other_cell_id = stable_uuid("agent-get-other-cell")
    await scenario.invoke(
        "cell.create",
        "founder",
        {
            "cell_id": other_cell_id,
            "name": "Other cell",
            "objective": "Stay outside the bound owner cell.",
            "budget_id": stable_uuid("agent-get-other-cell-budget"),
            "budget_limit": "100.00",
            "currency": "USD",
            "budget_duration_days": 90,
        },
        expected_revision=0,
        key="create-agent-get-other-cell",
    )
    other_instance_id = stable_uuid("agent-get-other-instance")
    await scenario.service.invoke(
        "agent.register",
        scenario.principals["founder"],
        mutation(
            "agent.register",
            {
                "agent_instance_id": other_instance_id,
                "principal_id": stable_uuid("agent-get-other-principal"),
                "role": "specialist",
                "cell_id": other_cell_id,
                "definition": load_definition("specialist"),
            },
            expected_revision=0,
            key="register-agent-get-other-instance",
        ),
    )
    reader = _cell_bound_reader()

    same_cell = await scenario.service.invoke(
        "agent.get",
        reader,
        {"agent_instance_id": SCOPED_READER_INSTANCE},
    )
    listed = await scenario.service.invoke("agent.list", reader, {})
    assert same_cell["agent"]["cell_id"] == CELL_ID
    assert same_cell["agent"]["agent_instance_id"] in {
        agent["agent_instance_id"] for agent in listed["agents"]
    }
    assert other_instance_id not in {agent["agent_instance_id"] for agent in listed["agents"]}

    with pytest.raises(AuthorizationError, match="does not cover") as cross_cell:
        await scenario.service.invoke(
            "agent.get",
            reader,
            {"agent_instance_id": other_instance_id},
        )
    assert other_instance_id not in str(cross_cell.value)
    assert "specialist" not in str(cross_cell.value)

    company_wide = await scenario.service.invoke(
        "agent.get",
        scenario.principals["observer"],
        {"agent_instance_id": other_instance_id},
    )
    founder = await scenario.service.invoke(
        "agent.get",
        scenario.principals["founder"],
        {"agent_instance_id": other_instance_id},
    )
    assert company_wide["agent"]["agent_instance_id"] == other_instance_id
    assert founder["agent"]["agent_instance_id"] == other_instance_id


@pytest.mark.asyncio
async def test_agent_get_denies_company_scoped_targets_to_cell_bound_callers() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    await _register_scoped_reader(scenario)
    immune_instance_id = stable_uuid("agent-get-immune-instance")
    await scenario.service.invoke(
        "agent.register",
        scenario.principals["founder"],
        mutation(
            "agent.register",
            {
                "agent_instance_id": immune_instance_id,
                "principal_id": stable_uuid("agent-get-immune-principal"),
                "role": "immune",
                "cell_id": None,
                "definition": load_definition("immune"),
            },
            expected_revision=0,
            key="register-agent-get-immune-instance",
        ),
    )
    reader = _cell_bound_reader()
    with pytest.raises(AuthorizationError, match="does not cover"):
        await scenario.service.invoke(
            "agent.get",
            reader,
            {"agent_instance_id": immune_instance_id},
        )
    fetched = await scenario.service.invoke(
        "agent.get",
        scenario.principals["founder"],
        {"agent_instance_id": immune_instance_id},
    )
    assert fetched["agent"]["cell_id"] is None
