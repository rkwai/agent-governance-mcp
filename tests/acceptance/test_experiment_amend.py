"""Owner-signed experiment.amend: extend a running window without retrofitting criteria."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ai_nature_company.errors import (
    AuthorizationError,
    BudgetError,
    ConflictError,
    ValidationError,
)
from ai_nature_company.experiment_stop import WINDOW_ELAPSED, parse_aware_datetime

from .scenario import Scenario
from .support import CELL_ID, COMPANY_ID, EXPERIMENT_ID, stable_uuid
from .test_experiment_record_send import (
    SEND_ID_TWO,
    _ready_to_record,
    _record,
    _send_payload,
)
from .test_natural_forces import _cell_ledger, _tries_running_experiment

FOREIGN_CELL_ID = stable_uuid("foreign-cell-for-amend")
FOREIGN_EXPERIMENT_ID = stable_uuid("foreign-experiment-for-amend")
CAP_DECISION_REF = "founder-decision:message-cap-equals-try-budget"
BUDGET_DECISION_REF = "founder-decision:extend-try-budget"

FOUNDER_DECISION_REF = "founder-decision:extend-acceptance-window"


def _window_end(experiment: dict[str, object]) -> str:
    window = experiment["window"]
    assert isinstance(window, dict)
    return str(window["ends_at"])


async def _amend(
    scenario: Scenario,
    actor: str,
    payload: dict[str, object],
    *,
    expected_revision: int | None = None,
    key: str = "amend-experiment",
) -> dict[str, object]:
    return await scenario.invoke(
        "experiment.amend",
        actor,
        payload,
        expected_revision=(
            scenario.experiment_revision() if expected_revision is None else expected_revision
        ),
        key=key,
    )


def _extend_payload(
    *,
    window_ends_at: str | None = None,
    stop_rules: list[dict[str, object]] | None = None,
    treatment: str | None = None,
    hypothesis: str | None = None,
    founder_decision_ref: str = FOUNDER_DECISION_REF,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "experiment_id": EXPERIMENT_ID,
        "founder_decision_ref": founder_decision_ref,
    }
    if window_ends_at is not None:
        payload["window_ends_at"] = window_ends_at
    if stop_rules is not None:
        payload["stop_rules"] = stop_rules
    if treatment is not None:
        payload["treatment"] = treatment
    if hypothesis is not None:
        payload["hypothesis"] = hypothesis
    if extra:
        payload.update(extra)
    return payload


@pytest.mark.asyncio
async def test_owner_extends_running_window_and_retains_amendment_lineage() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    started = scenario.responses[f"experiment.start:{EXPERIMENT_ID}"]["state"]["experiment"]
    prior_end = _window_end(started)
    prior_treatment = started["treatment"]
    prior_rules = started["stop_rules"]
    new_end = (scenario.clock() + timedelta(days=21)).isoformat().replace("+00:00", "Z")
    hypothesis = started["hypothesis"]
    metrics = started["metrics"]
    success = started["success_thresholds"]
    budget = started["budget"]

    amended = await _amend(
        scenario,
        "owner",
        _extend_payload(window_ends_at=new_end),
        expected_revision=2,
        key="amend-window",
    )

    experiment = amended["state"]["experiment"]
    assert amended["prior_revision"] == 2
    assert amended["new_revision"] == 3
    assert experiment["revision"] == 3
    assert experiment["state"] == "running"
    assert experiment["experiment_id"] == EXPERIMENT_ID
    assert experiment["cell_id"] == CELL_ID
    assert _window_end(experiment) == new_end
    assert experiment["window"]["starts_at"] == started["window"]["starts_at"]
    assert experiment["treatment"] == prior_treatment
    assert experiment["stop_rules"] == prior_rules
    assert experiment["hypothesis"] == hypothesis
    assert experiment["metrics"] == metrics
    assert experiment["success_thresholds"] == success
    assert experiment["failure_thresholds"] == started["failure_thresholds"]
    assert experiment["budget"] == budget
    assert experiment["evaluator_principal_id"] == started["evaluator_principal_id"]
    assert experiment["offer_version_id"] == started["offer_version_id"]
    history = experiment["amendment_history"]
    assert len(history) == 1
    record = history[0]
    assert record["founder_decision_ref"] == FOUNDER_DECISION_REF
    assert record["prior"]["window"]["ends_at"] == prior_end
    assert record["new"]["window"]["ends_at"] == new_end
    assert record["prior"]["treatment"] == prior_treatment
    assert record["prior"]["stop_rules"] == prior_rules
    assert record["changed_fields"] == ["window"]

    decision = amended["state"]["decision"]
    explained = await scenario.service.invoke(
        "decision.explain",
        scenario.principals["owner"],
        {"decision_id": decision["decision_id"]},
    )
    shown = explained["decision"]
    assert shown["kind"] == "experiment.amend"
    assert shown["founder_decision_ref"] == FOUNDER_DECISION_REF
    assert shown["prior"]["window"]["ends_at"] == prior_end
    assert shown["new"]["window"]["ends_at"] == new_end
    assert shown["changed_fields"] == ["window"]

    audit = await scenario.service.invoke(
        "audit.list",
        scenario.principals["founder"],
        {"limit": 1_000},
    )
    event = next(item for item in audit["audit_events"] if item["action"] == "experiment.amend")
    assert event["details"]["founder_decision_ref"] == FOUNDER_DECISION_REF
    assert event["details"]["prior"]["window"]["ends_at"] == prior_end
    assert event["details"]["new"]["window"]["ends_at"] == new_end
    assert event["details"]["decision_id"] == decision["decision_id"]


@pytest.mark.asyncio
async def test_tick_does_not_close_at_old_end_and_does_close_after_amended_end() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    new_end = (scenario.clock() + timedelta(days=21)).isoformat().replace("+00:00", "Z")
    await _amend(
        scenario,
        "owner",
        _extend_payload(window_ends_at=new_end),
        expected_revision=2,
        key="amend-before-tick",
    )

    scenario.clock.advance(days=8)
    mid = await scenario.service.tick(COMPANY_ID)
    assert mid["closed_experiment_ids"] == []
    viewed = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    due = next(item for item in viewed["experiments"] if item["experiment_id"] == EXPERIMENT_ID)
    assert due["state"] == "running"
    assert due["stop_state"] == "clear"
    assert due["close_eligible"] is False
    assert due["window_ends_at"] == new_end

    scenario.clock.advance(days=14)
    after = await scenario.service.tick(COMPANY_ID)
    assert after["closed_experiment_ids"] == [EXPERIMENT_ID]
    closed = await scenario.service.invoke(
        "cell.get",
        scenario.principals["owner"],
        {"cell_id": CELL_ID},
    )
    done = next(item for item in closed["experiments"] if item["experiment_id"] == EXPERIMENT_ID)
    assert done["state"] == "evaluating"
    assert done["stop_state"] == "fired"
    assert WINDOW_ELAPSED in {rule["rule_id"] for rule in done["fired_rules"]}


@pytest.mark.asyncio
async def test_amend_rejects_draft_registered_and_closed_experiments() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    await scenario.register_experiment()
    later = (scenario.clock() + timedelta(days=21)).isoformat().replace("+00:00", "Z")
    with pytest.raises(ValidationError, match="only a running experiment can be amended"):
        await _amend(
            scenario,
            "owner",
            _extend_payload(window_ends_at=later),
            expected_revision=1,
            key="amend-registered",
        )

    async with scenario.store.transaction(COMPANY_ID) as tx:
        record = await tx.get_projection("experiment", EXPERIMENT_ID)
        assert record is not None
        draft = dict(record["state"])
        draft["state"] = "draft"
        await tx.put_projection(
            projection_type="experiment",
            projection_id=EXPERIMENT_ID,
            state=draft,
            expected_revision=record["revision"],
            updated_at=scenario.clock(),
        )
    with pytest.raises(ValidationError, match="only a running experiment can be amended"):
        await _amend(
            scenario,
            "owner",
            _extend_payload(window_ends_at=later),
            expected_revision=1,
            key="amend-draft",
        )

    closed = Scenario()
    await closed.build_evaluating_experiment()
    with pytest.raises(ValidationError, match="only a running experiment can be amended"):
        await _amend(
            closed,
            "owner",
            _extend_payload(
                window_ends_at=(closed.clock() + timedelta(days=21))
                .isoformat()
                .replace("+00:00", "Z")
            ),
            key="amend-closed",
        )


@pytest.mark.asyncio
async def test_amend_refuses_specialist_evaluator_and_founder() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    later = (scenario.clock() + timedelta(days=21)).isoformat().replace("+00:00", "Z")
    for actor, key in (
        ("specialist", "amend-as-specialist"),
        ("evaluator", "amend-as-evaluator"),
        ("founder", "amend-as-founder"),
    ):
        with pytest.raises(AuthorizationError):
            await _amend(
                scenario,
                actor,
                _extend_payload(window_ends_at=later),
                expected_revision=2,
                key=key,
            )


@pytest.mark.asyncio
async def test_amend_rejects_shortening_past_end_noop_and_frozen_fields() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    started = scenario.responses[f"experiment.start:{EXPERIMENT_ID}"]["state"]["experiment"]
    current_end = _window_end(started)
    shorter = (scenario.clock() + timedelta(days=3)).isoformat().replace("+00:00", "Z")

    with pytest.raises(ValidationError, match="later than the current window end"):
        await _amend(
            scenario,
            "owner",
            _extend_payload(window_ends_at=shorter),
            expected_revision=2,
            key="amend-shorten",
        )

    with pytest.raises(
        ValidationError,
        match=(
            "window_ends_at, stop_rules, treatment, hypothesis, "
            "budget_amount_minor, or max_external_messages"
        ),
    ):
        await _amend(
            scenario,
            "owner",
            _extend_payload(window_ends_at=current_end),
            expected_revision=2,
            key="amend-noop",
        )

    with pytest.raises(ValidationError, match="founder_decision_ref is required"):
        await scenario.invoke(
            "experiment.amend",
            "owner",
            {"experiment_id": EXPERIMENT_ID, "window_ends_at": current_end},
            expected_revision=2,
            key="amend-missing-ref",
        )

    later = (scenario.clock() + timedelta(days=21)).isoformat().replace("+00:00", "Z")
    with pytest.raises(ValidationError, match="cannot change frozen"):
        await _amend(
            scenario,
            "owner",
            _extend_payload(
                window_ends_at=later,
                extra={"success_thresholds": started["success_thresholds"]},
            ),
            expected_revision=2,
            key="amend-thresholds",
        )
    with pytest.raises(ValidationError, match="cannot change frozen"):
        await _amend(
            scenario,
            "owner",
            _extend_payload(window_ends_at=later, extra={"budget": started["budget"]}),
            expected_revision=2,
            key="amend-budget",
        )


@pytest.mark.asyncio
async def test_amend_rejects_a_new_end_that_is_not_later_than_now() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    current_end = parse_aware_datetime(
        _window_end(scenario.responses[f"experiment.start:{EXPERIMENT_ID}"]["state"]["experiment"]),
        "window.ends_at",
    )
    scenario.clock.advance(days=8)
    still_past_now = (current_end + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    with pytest.raises(ValidationError, match="later than now"):
        await _amend(
            scenario,
            "owner",
            _extend_payload(window_ends_at=still_past_now),
            expected_revision=2,
            key="amend-past-now",
        )


@pytest.mark.asyncio
async def test_amend_can_revise_stop_rules_or_treatment_without_moving_the_window() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    started = scenario.responses[f"experiment.start:{EXPERIMENT_ID}"]["state"]["experiment"]
    revised_rules = [
        {
            "rule_id": "budget_exhausted",
            "condition": "Actual cost reaches the fixed experiment budget.",
            "effect": "pause_external_actions",
        },
        {
            "rule_id": "window_elapsed",
            "condition": "The amended evaluation window has ended.",
            "effect": "pause_external_actions",
        },
    ]
    first = await _amend(
        scenario,
        "owner",
        _extend_payload(stop_rules=revised_rules),
        expected_revision=2,
        key="amend-stop-rules",
    )
    experiment = first["state"]["experiment"]
    assert experiment["stop_rules"] == revised_rules
    assert _window_end(experiment) == _window_end(started)
    assert first["state"]["decision"]["changed_fields"] == ["stop_rules"]

    second = await _amend(
        scenario,
        "owner",
        _extend_payload(treatment="Deliver a bounded sample to twelve eligible firms."),
        expected_revision=3,
        key="amend-treatment",
    )
    updated = second["state"]["experiment"]
    assert updated["treatment"] == "Deliver a bounded sample to twelve eligible firms."
    assert updated["stop_rules"] == revised_rules
    assert len(updated["amendment_history"]) == 2
    assert updated["amendment_history"][0]["prior"]["stop_rules"] == started["stop_rules"]
    assert updated["amendment_history"][1]["prior"]["treatment"] == started["treatment"]


@pytest.mark.asyncio
async def test_amend_is_idempotent_and_rejects_stale_revision() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    later = (scenario.clock() + timedelta(days=21)).isoformat().replace("+00:00", "Z")
    payload = _extend_payload(window_ends_at=later)
    first = await _amend(scenario, "owner", payload, expected_revision=2, key="amend-once")
    replay = await _amend(scenario, "owner", payload, expected_revision=2, key="amend-once")
    assert replay["replayed"] is True
    assert replay["new_revision"] == first["new_revision"]
    assert replay["state"]["experiment"]["window"] == first["state"]["experiment"]["window"]

    with pytest.raises(ConflictError, match="stale"):
        await _amend(
            scenario,
            "owner",
            _extend_payload(
                window_ends_at=(scenario.clock() + timedelta(days=28))
                .isoformat()
                .replace("+00:00", "Z")
            ),
            expected_revision=2,
            key="amend-stale",
        )


@pytest.mark.asyncio
async def test_protocol_describe_advertises_owner_signed_experiment_amend() -> None:
    scenario = Scenario()
    await scenario.bootstrap()
    described = await scenario.service.invoke(
        "protocol.describe",
        scenario.principals["owner"],
        {},
    )
    assert "experiment.amend" in described["tools"]
    workflow = described["bet_workflow"]
    amend = workflow["experiment_amend"]
    assert amend["tool"] == "experiment.amend"
    assert amend["authority"] == "owner_signed"
    assert amend["requires"] == "founder_decision_ref"
    assert amend["valid_state"] == "running"
    assert amend["must_not"] == [
        "retrofit_evaluation_criteria",
        "change_budget_unit",
        "change_evaluator_or_cell",
        "amend_constitution_or_risk_policy",
    ]
    assert "align_hypothesis_window_text" in amend["may"]
    assert "set_max_external_messages" in amend["may"]
    assert "set_budget_amount_minor" in amend["may"]
    assert amend["hypothesis_dates_are_not"] == "the_clock"
    assert amend["message_cap"]["field"] == "max_external_messages"
    assert amend["budget_amount"]["path"] == "reservation_ledger"
    assert workflow["evaluation"]["judges_window_against"] == "window_ends_at"
    assert workflow["evaluation"]["does_not_parse"] == "hypothesis_dates"
    assert any("experiment.amend" in step for step in workflow["operate"])
    assert any("Do not DM a peer" in step for step in workflow["operate"])
    assert any(
        "not silence toward founder" in step and "reply when they are waiting" in step
        for step in workflow["operate"]
    )


@pytest.mark.asyncio
async def test_owner_can_align_hypothesis_window_text_without_retrofitting_criteria() -> None:
    scenario = Scenario()
    await scenario.build_running_experiment()
    started = scenario.responses[f"experiment.start:{EXPERIMENT_ID}"]["state"]["experiment"]
    prior = started["hypothesis"]
    aligned = "Ten verified samples will produce one paid-pilot request before the amended window."
    amended = await _amend(
        scenario,
        "owner",
        _extend_payload(hypothesis=aligned),
        expected_revision=2,
        key="amend-hypothesis-window",
    )
    experiment = amended["state"]["experiment"]
    assert experiment["hypothesis"] == aligned
    assert experiment["metrics"] == started["metrics"]
    assert experiment["success_thresholds"] == started["success_thresholds"]
    assert experiment["failure_thresholds"] == started["failure_thresholds"]
    assert experiment["budget"] == started["budget"]
    assert experiment["evaluator_principal_id"] == started["evaluator_principal_id"]
    assert _window_end(experiment) == _window_end(started)
    history = experiment["amendment_history"]
    assert history[0]["changed_fields"] == ["hypothesis"]
    assert history[0]["prior"]["hypothesis"] == prior
    assert history[0]["new"]["hypothesis"] == aligned
    assert amended["state"]["decision"]["changed_fields"] == ["hypothesis"]


@pytest.mark.asyncio
async def test_evaluation_judges_window_ends_at_not_hypothesis_dates() -> None:
    scenario = Scenario()
    await scenario.build_evaluating_experiment()
    experiment = scenario.responses[f"experiment.start:{EXPERIMENT_ID}"]["state"]["experiment"]
    current_end = _window_end(experiment)
    stale_end = parse_aware_datetime(current_end, "window.ends_at") - timedelta(days=14)
    with pytest.raises(ValidationError, match="window_ends_at"):
        await scenario.submit_evaluation(
            window_ends_at=stale_end.isoformat().replace("+00:00", "Z"),
            key="evaluate-against-hypothesis-date",
        )
    accepted = await scenario.submit_evaluation(key="evaluate-against-window")
    assert accepted["state"]["judged_window_ends_at"] == current_end
    assert accepted["state"]["experiment"]["state"] == "passed"


@pytest.mark.asyncio
async def test_amend_sets_message_cap_and_is_visible_on_audit_decision_describe() -> None:
    scenario = await _tries_running_experiment()
    constitution = await scenario.service.invoke(
        "constitution.get",
        scenario.principals["owner"],
        {},
    )
    assert constitution["constitution"]["risk_policy"]["max_external_messages_per_experiment"] == 10

    amended = await _amend(
        scenario,
        "owner",
        {
            "experiment_id": EXPERIMENT_ID,
            "max_external_messages": 4,
            "founder_decision_ref": CAP_DECISION_REF,
        },
        expected_revision=2,
        key="amend-cap-to-budget",
    )
    experiment = amended["state"]["experiment"]
    assert experiment["max_external_messages"] == 4
    assert experiment["budget"]["amount_minor"] == 4
    assert experiment["amendment_history"][-1]["prior"]["max_external_messages"] is None
    assert experiment["amendment_history"][-1]["new"]["max_external_messages"] == 4
    assert experiment["amendment_history"][-1]["changed_fields"] == ["max_external_messages"]

    described = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    bet = described["cell"]["experiments"][0]
    assert bet["max_external_messages"] == 4
    assert bet["max_external_messages_per_experiment"] == 4
    assert bet["external_messages_remaining"] == 4

    page = await scenario.service.invoke(
        "company.describe",
        scenario.principals["owner"],
        {"cell_records": {"cell_id": CELL_ID, "kind": "experiment"}},
    )
    stored = page["cell_records"]["records"][0]["state"]
    assert stored["max_external_messages"] == 4
    assert stored["amendment_history"][-1]["founder_decision_ref"] == CAP_DECISION_REF

    decision = amended["state"]["decision"]
    explained = await scenario.service.invoke(
        "decision.explain",
        scenario.principals["owner"],
        {"decision_id": decision["decision_id"]},
    )
    assert explained["decision"]["new"]["max_external_messages"] == 4
    assert explained["decision"]["changed_fields"] == ["max_external_messages"]

    audit = await scenario.service.invoke(
        "audit.list",
        scenario.principals["founder"],
        {"limit": 1_000},
    )
    event = next(item for item in audit["audit_events"] if item["action"] == "experiment.amend")
    assert event["details"]["new"]["max_external_messages"] == 4
    assert event["details"]["founder_decision_ref"] == CAP_DECISION_REF

    after = await scenario.service.invoke(
        "constitution.get",
        scenario.principals["owner"],
        {},
    )
    assert after["constitution"]["risk_policy"]["max_external_messages_per_experiment"] == 10
    assert after["constitution"]["version"] == constitution["constitution"]["version"]


@pytest.mark.asyncio
async def test_amend_message_cap_ceiling_is_try_budget() -> None:
    scenario = await _tries_running_experiment()
    with pytest.raises(ValidationError, match="cannot exceed the experiment try budget"):
        await _amend(
            scenario,
            "owner",
            {
                "experiment_id": EXPERIMENT_ID,
                "max_external_messages": 5,
                "founder_decision_ref": CAP_DECISION_REF,
            },
            expected_revision=2,
            key="amend-cap-above-budget",
        )


@pytest.mark.asyncio
async def test_amend_message_cap_floor_is_recorded_sends() -> None:
    scenario = await _ready_to_record()
    await _record(scenario, "owner", _send_payload(), key="send-before-cap-floor")
    await _record(
        scenario,
        "owner",
        _send_payload(send_id=SEND_ID_TWO, target="Globex", sent_at="2026-09-21T17:00:00Z"),
        key="send-two-before-cap-floor",
    )
    with pytest.raises(ValidationError, match="cannot drop below recorded sends"):
        await _amend(
            scenario,
            "owner",
            {
                "experiment_id": EXPERIMENT_ID,
                "max_external_messages": 1,
                "founder_decision_ref": CAP_DECISION_REF,
            },
            expected_revision=2,
            key="amend-cap-below-sends",
        )
    lowered = await _amend(
        scenario,
        "owner",
        {
            "experiment_id": EXPERIMENT_ID,
            "max_external_messages": 2,
            "founder_decision_ref": CAP_DECISION_REF,
        },
        expected_revision=2,
        key="amend-cap-to-recorded",
    )
    assert lowered["state"]["experiment"]["max_external_messages"] == 2


@pytest.mark.asyncio
async def test_amend_refuses_a_running_experiment_in_another_cell() -> None:
    scenario = await _tries_running_experiment()
    async with scenario.store.transaction(COMPANY_ID) as tx:
        record = await tx.get_projection("experiment", EXPERIMENT_ID)
        assert record is not None
        foreign = dict(record["state"])
        foreign["experiment_id"] = FOREIGN_EXPERIMENT_ID
        foreign["cell_id"] = FOREIGN_CELL_ID
        await tx.put_projection(
            projection_type="experiment",
            projection_id=FOREIGN_EXPERIMENT_ID,
            state=foreign,
            expected_revision=0,
            updated_at=scenario.clock(),
        )
    with pytest.raises(AuthorizationError, match="does not cover this cell"):
        await scenario.invoke(
            "experiment.amend",
            "owner",
            {
                "experiment_id": FOREIGN_EXPERIMENT_ID,
                "max_external_messages": 4,
                "founder_decision_ref": CAP_DECISION_REF,
            },
            expected_revision=2,
            key="amend-wrong-cell",
        )


@pytest.mark.asyncio
async def test_amend_message_cap_requires_running_and_founder_ref() -> None:
    scenario = Scenario()
    await scenario.build_offer_cell()
    await scenario.register_experiment()
    with pytest.raises(ValidationError, match="only a running experiment can be amended"):
        await _amend(
            scenario,
            "owner",
            {
                "experiment_id": EXPERIMENT_ID,
                "max_external_messages": 4,
                "founder_decision_ref": CAP_DECISION_REF,
            },
            expected_revision=1,
            key="amend-cap-registered",
        )

    running = await _tries_running_experiment()
    with pytest.raises(ValidationError, match="founder_decision_ref is required"):
        await running.invoke(
            "experiment.amend",
            "owner",
            {"experiment_id": EXPERIMENT_ID, "max_external_messages": 4},
            expected_revision=2,
            key="amend-cap-missing-ref",
        )


@pytest.mark.asyncio
async def test_amend_extends_budget_through_reservation_ledger() -> None:
    scenario = Scenario()
    await scenario.bootstrap(
        constitution_unit="tries",
        constitution_currency=None,
        company_limit_minor=80,
        cell_limit_minor=40,
        experiment_limit_minor=20,
        single_action_limit_minor=20,
        budget_unit="tries",
        budget_currency=None,
        budget_limit=80,
    )
    await scenario.create_cell(budget_limit=40, unit="tries", currency=None)
    await scenario.register_offer()
    await scenario.mutate_offer()
    await scenario.activate_cell()
    await scenario.register_experiment(budget={"unit": "tries", "amount_minor": 4})
    await scenario.start_experiment()

    before = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert before["cell"]["experiments"][0]["remaining_budget_minor"] == 4
    assert before["ledger"]["reserved_minor"] == 4

    extended = await _amend(
        scenario,
        "owner",
        {
            "experiment_id": EXPERIMENT_ID,
            "budget_amount_minor": 12,
            "founder_decision_ref": BUDGET_DECISION_REF,
        },
        expected_revision=2,
        key="amend-extend-budget",
    )
    experiment = extended["state"]["experiment"]
    assert experiment["budget"] == {"unit": "tries", "amount_minor": 12}
    assert experiment["amendment_history"][-1]["prior"]["budget"]["amount_minor"] == 4
    assert experiment["amendment_history"][-1]["changed_fields"] == ["budget_amount_minor"]
    assert extended["state"]["decision"]["reservation_amount_minor"] == 12

    after = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after["cell"]["experiments"][0]["remaining_budget_minor"] == 12
    assert after["ledger"]["reserved_minor"] == 12
    assert after["ledger"]["spent_minor"] == 0


@pytest.mark.asyncio
async def test_amend_budget_floor_at_spent() -> None:
    scenario = await _ready_to_record()
    await _record(scenario, "owner", _send_payload(), key="spend-one-for-floor")
    await _record(
        scenario,
        "owner",
        _send_payload(send_id=SEND_ID_TWO, target="Globex", sent_at="2026-09-21T17:00:00Z"),
        key="spend-two-for-floor",
    )
    with pytest.raises(BudgetError, match="cannot drop below already spent"):
        await _amend(
            scenario,
            "owner",
            {
                "experiment_id": EXPERIMENT_ID,
                "budget_amount_minor": 1,
                "founder_decision_ref": BUDGET_DECISION_REF,
            },
            expected_revision=2,
            key="amend-budget-below-spent",
        )
    at_floor = await _amend(
        scenario,
        "owner",
        {
            "experiment_id": EXPERIMENT_ID,
            "budget_amount_minor": 2,
            "founder_decision_ref": BUDGET_DECISION_REF,
        },
        expected_revision=2,
        key="amend-budget-to-spent",
    )
    assert at_floor["state"]["experiment"]["budget"]["amount_minor"] == 2
    after = _cell_ledger(
        await scenario.service.invoke("company.describe", scenario.principals["owner"], {})
    )
    assert after["cell"]["experiments"][0]["remaining_budget_minor"] == 0
    assert after["ledger"]["reserved_minor"] == 0
    assert after["ledger"]["spent_minor"] == 2


@pytest.mark.asyncio
async def test_amend_refuses_budget_unit_evaluator_and_cell_changes() -> None:
    scenario = await _tries_running_experiment()
    started = scenario.responses[f"experiment.start:{EXPERIMENT_ID}"]["state"]["experiment"]
    with pytest.raises(ValidationError, match="cannot change frozen"):
        await _amend(
            scenario,
            "owner",
            {
                "experiment_id": EXPERIMENT_ID,
                "founder_decision_ref": BUDGET_DECISION_REF,
                "budget": {"unit": "money", "currency": "USD", "amount_minor": 4},
            },
            expected_revision=2,
            key="amend-budget-object",
        )
    with pytest.raises(ValidationError, match="cannot change frozen"):
        await _amend(
            scenario,
            "owner",
            {
                "experiment_id": EXPERIMENT_ID,
                "founder_decision_ref": BUDGET_DECISION_REF,
                "evaluator_principal_id": started["evaluator_principal_id"],
            },
            expected_revision=2,
            key="amend-evaluator",
        )
    with pytest.raises(ValidationError, match="cannot change frozen"):
        await _amend(
            scenario,
            "owner",
            {
                "experiment_id": EXPERIMENT_ID,
                "founder_decision_ref": BUDGET_DECISION_REF,
                "cell_id": CELL_ID,
            },
            expected_revision=2,
            key="amend-cell",
        )


@pytest.mark.asyncio
async def test_amend_cap_ceiling_follows_amended_budget() -> None:
    scenario = Scenario()
    await scenario.bootstrap(
        constitution_unit="tries",
        constitution_currency=None,
        company_limit_minor=80,
        cell_limit_minor=40,
        experiment_limit_minor=20,
        single_action_limit_minor=20,
        budget_unit="tries",
        budget_currency=None,
        budget_limit=80,
    )
    await scenario.create_cell(budget_limit=40, unit="tries", currency=None)
    await scenario.register_offer()
    await scenario.mutate_offer()
    await scenario.activate_cell()
    await scenario.register_experiment(budget={"unit": "tries", "amount_minor": 4})
    await scenario.start_experiment()

    with pytest.raises(ValidationError, match="cannot exceed the experiment try budget"):
        await _amend(
            scenario,
            "owner",
            {
                "experiment_id": EXPERIMENT_ID,
                "max_external_messages": 8,
                "founder_decision_ref": CAP_DECISION_REF,
            },
            expected_revision=2,
            key="amend-cap-before-extend",
        )

    both = await _amend(
        scenario,
        "owner",
        {
            "experiment_id": EXPERIMENT_ID,
            "budget_amount_minor": 8,
            "max_external_messages": 8,
            "founder_decision_ref": CAP_DECISION_REF,
        },
        expected_revision=2,
        key="amend-budget-then-cap",
    )
    assert both["state"]["experiment"]["budget"]["amount_minor"] == 8
    assert both["state"]["experiment"]["max_external_messages"] == 8
    assert set(both["state"]["decision"]["changed_fields"]) == {
        "budget_amount_minor",
        "max_external_messages",
    }
