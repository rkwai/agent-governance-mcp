from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from ai_nature_company.errors import (
    BudgetError,
    ConflictError,
    LeaseError,
    NotFoundError,
    ValidationError,
)
from ai_nature_company.store import InMemoryStore, StoreTransaction

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


async def _append(
    store: InMemoryStore,
    company_id: str,
    *,
    event_id: str,
    expected_revision: int | None = None,
    value: int = 1,
) -> dict[str, object]:
    async with store.transaction(company_id) as transaction:
        return await transaction.append_event(
            aggregate_type="offer",
            aggregate_id="offer-1",
            event_type="offer.changed",
            actor_id="agent-1",
            payload={"value": value},
            event_id=event_id,
            expected_revision=expected_revision,
            occurred_at=NOW,
        )


async def test_transaction_rolls_back_every_write() -> None:
    store = InMemoryStore()

    with pytest.raises(RuntimeError, match="abort"):
        async with store.transaction("company-a") as transaction:
            await transaction.append_event(
                aggregate_type="offer",
                aggregate_id="offer-1",
                event_type="offer.created",
                actor_id="founder",
                payload={},
                event_id="event-1",
                occurred_at=NOW,
            )
            await transaction.put_projection(
                projection_type="offer",
                projection_id="offer-1",
                state={"status": "draft"},
                expected_revision=0,
                updated_at=NOW,
            )
            raise RuntimeError("abort")

    exported = await store.export_company("company-a")
    assert exported["domain_events"] == []
    assert exported["projections"] == []


async def test_transaction_object_cannot_be_used_after_exit() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        assert transaction.company_id == "company-a"

    with pytest.raises(RuntimeError, match="closed"):
        await transaction.list_events()


async def test_nested_memory_transactions_fail_instead_of_deadlocking() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a"):
        with pytest.raises(RuntimeError, match="nested"):
            async with store.transaction("company-a"):
                pass


async def test_company_scope_is_explicit_and_export_is_isolated() -> None:
    store = InMemoryStore()
    await _append(store, "company-a", event_id="event-a")
    await _append(store, "company-b", event_id="event-b")
    async with store.transaction("company-a") as transaction:
        await transaction.put_projection(
            projection_type="offer",
            projection_id="offer-a",
            state={"company": "a"},
            expected_revision=0,
            updated_at=NOW,
        )
    async with store.transaction("company-b") as transaction:
        await transaction.put_projection(
            projection_type="offer",
            projection_id="offer-b",
            state={"company": "b"},
            expected_revision=0,
            updated_at=NOW,
        )

    company_a = await store.export_company("company-a")
    company_b = await store.export_company("company-b")

    assert [event["event_id"] for event in company_a["domain_events"]] == ["event-a"]
    assert [item["projection_id"] for item in company_a["projections"]] == ["offer-a"]
    assert [event["event_id"] for event in company_b["domain_events"]] == ["event-b"]
    assert [item["projection_id"] for item in company_b["projections"]] == ["offer-b"]


async def test_events_are_append_only_revisioned_idempotent_and_replayable() -> None:
    store = InMemoryStore()
    first = await _append(store, "company-a", event_id="event-1", expected_revision=0, value=2)
    second = await _append(store, "company-a", event_id="event-2", expected_revision=1, value=3)
    retry = await _append(store, "company-a", event_id="event-2", expected_revision=1, value=3)

    assert first["aggregate_revision"] == 1
    assert second["aggregate_revision"] == 2
    assert retry == second
    assert first["position"] < second["position"]

    total = await store.replay(
        "company-a",
        lambda state, event: state + event["payload"]["value"],
        0,
        aggregate_type="offer",
        aggregate_id="offer-1",
    )
    assert total == 5

    with pytest.raises(ConflictError, match="stale aggregate"):
        await _append(store, "company-a", event_id="event-3", expected_revision=1)
    with pytest.raises(ConflictError, match="another event"):
        await _append(store, "company-a", event_id="event-2", value=99)


async def test_event_and_audit_inputs_are_defensively_copied() -> None:
    store = InMemoryStore()
    payload = {"nested": {"value": 1}}
    details = {"connector": {"status": "ok"}}
    async with store.transaction("company-a") as transaction:
        event = await transaction.append_event(
            aggregate_type="evidence",
            aggregate_id="evidence-1",
            event_type="evidence.appended",
            actor_id="agent-1",
            payload=payload,
            event_id="event-1",
            occurred_at=NOW,
        )
        audit = await transaction.append_audit_event(
            audit_id="audit-1",
            action="connector.send",
            outcome="succeeded",
            actor_id="agent-1",
            resource_type="message",
            resource_id="message-1",
            details=details,
            occurred_at=NOW,
        )
        payload["nested"]["value"] = 999
        details["connector"]["status"] = "tampered"
        event["payload"]["nested"]["value"] = 888
        audit["details"]["connector"]["status"] = "tampered-again"

    exported = await store.export_company("company-a")
    assert exported["domain_events"][0]["payload"] == {"nested": {"value": 1}}
    assert exported["audit_events"][0]["details"] == {"connector": {"status": "ok"}}


async def test_projection_requires_optimistic_revision_and_lists_stably() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        one = await transaction.put_projection(
            projection_type="offer",
            projection_id="b",
            state={"status": "draft"},
            expected_revision=0,
            updated_at=NOW,
        )
        await transaction.put_projection(
            projection_type="offer",
            projection_id="a",
            state={"status": "active"},
            expected_revision=0,
            updated_at=NOW,
        )
        updated = await transaction.put_projection(
            projection_type="offer",
            projection_id="b",
            state={"status": "active"},
            expected_revision=one["revision"],
            updated_at=NOW + timedelta(seconds=1),
        )
        listed = await transaction.list_projections(projection_type="offer", limit=10)

        assert updated["revision"] == 2
        assert [record["projection_id"] for record in listed] == ["a", "b"]
        with pytest.raises(ConflictError, match="stale projection"):
            await transaction.put_projection(
                projection_type="offer",
                projection_id="b",
                state={},
                expected_revision=1,
            )


async def test_idempotency_claim_and_outcome_are_immutable() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        claim = await transaction.claim_idempotency(
            namespace="connector.send",
            key="request-1",
            request_hash="hash-a",
            now=NOW,
        )
        completed = await transaction.complete_idempotency(
            namespace="connector.send",
            key="request-1",
            request_hash="hash-a",
            response={"external_id": "message-1"},
            now=NOW,
        )
        retry = await transaction.claim_idempotency(
            namespace="connector.send",
            key="request-1",
            request_hash="hash-a",
            now=NOW,
        )
        assert claim["status"] == "in_progress"
        assert completed["status"] == "completed"
        assert retry == completed

        with pytest.raises(ConflictError, match="different request"):
            await transaction.claim_idempotency(
                namespace="connector.send",
                key="request-1",
                request_hash="hash-b",
                now=NOW,
            )
        with pytest.raises(ConflictError, match="immutable"):
            await transaction.complete_idempotency(
                namespace="connector.send",
                key="request-1",
                request_hash="hash-a",
                response={"external_id": "message-2"},
                now=NOW,
            )


async def test_expired_idempotency_claim_can_be_reused() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        await transaction.claim_idempotency(
            namespace="tool",
            key="key",
            request_hash="old",
            expires_at=NOW + timedelta(minutes=1),
            now=NOW,
        )
    async with store.transaction("company-a") as transaction:
        replacement = await transaction.claim_idempotency(
            namespace="tool",
            key="key",
            request_hash="new",
            expires_at=NOW + timedelta(minutes=3),
            now=NOW + timedelta(minutes=2),
        )
    assert replacement["request_hash"] == "new"
    assert replacement["revision"] == 2


async def test_export_redacts_tokens_nested_in_idempotency_receipts() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        await transaction.claim_idempotency(
            namespace="work.claim:principal",
            key="claim-1",
            request_hash="request-hash",
            now=NOW,
        )
        await transaction.complete_idempotency(
            namespace="work.claim:principal",
            key="claim-1",
            request_hash="request-hash",
            response={
                "state": {
                    "lease": {
                        "token": "live-bearer-capability",
                        "lease_token": "duplicate-capability",
                        "lease_id": "lease-1",
                    }
                }
            },
            now=NOW,
        )

    exported = await store.export_company("company-a")
    serialized = repr(exported["idempotency_records"])
    assert "live-bearer-capability" not in serialized
    assert "duplicate-capability" not in serialized
    assert exported["idempotency_records"][0]["response"]["state"]["lease"] == {
        "lease_id": "lease-1"
    }


async def test_atomic_idempotency_lock_spans_claim_and_mutation() -> None:
    store = InMemoryStore()
    mutation_count = 0

    async def perform() -> dict[str, object]:
        nonlocal mutation_count
        async with store.transaction("company-a") as transaction:
            claim = await transaction.claim_idempotency(
                namespace="action",
                key="same-key",
                request_hash="same-hash",
                now=NOW,
            )
            if claim["status"] == "completed":
                return claim
            mutation_count += 1
            await asyncio.sleep(0)
            return await transaction.complete_idempotency(
                namespace="action",
                key="same-key",
                request_hash="same-hash",
                response={"ok": True},
                now=NOW,
            )

    first, second = await asyncio.gather(perform(), perform())
    assert first == second
    assert mutation_count == 1


async def test_budget_reserve_commit_release_and_overspend() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        budget = await transaction.create_budget(
            budget_id="budget-1",
            scope="experiment",
            limit="100.00",
            currency="usd",
            experiment_id="experiment-1",
            starts_at=NOW,
            ends_at=NOW + timedelta(days=1),
            now=NOW,
        )
        assert budget["limit"] == Decimal("100.00")
        first = await transaction.reserve_budget(
            budget_id="budget-1",
            reservation_id="reservation-1",
            amount="60.00",
            purpose="ads",
            expected_revision=0,
            now=NOW,
        )
        retry = await transaction.reserve_budget(
            budget_id="budget-1",
            reservation_id="reservation-1",
            amount="60.00",
            purpose="ads",
            expected_revision=0,
            now=NOW,
        )
        assert retry == first
        with pytest.raises(BudgetError, match="available budget"):
            await transaction.reserve_budget(
                budget_id="budget-1",
                reservation_id="reservation-too-large",
                amount="41.00",
                purpose="ads",
                now=NOW,
            )
        committed = await transaction.commit_reservation(
            reservation_id="reservation-1",
            actual_amount="50.00",
            expected_revision=0,
            now=NOW,
        )
        await transaction.reserve_budget(
            budget_id="budget-1",
            reservation_id="reservation-2",
            amount="20.00",
            purpose="tools",
            now=NOW,
        )
        released = await transaction.release_reservation(
            reservation_id="reservation-2",
            now=NOW,
        )
        final_budget = await transaction.get_budget("budget-1")

    assert committed["status"] == "committed"
    assert released["status"] == "released"
    assert final_budget["spent"] == Decimal("50.00")
    assert final_budget["reserved"] == Decimal("0.00")


async def test_update_budget_can_close_or_resize_without_changing_unit() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        created = await transaction.create_budget(
            budget_id="budget-1",
            scope="company",
            limit=20,
            unit="tries",
            now=NOW,
        )
        resized = await transaction.update_budget(
            budget_id="budget-1",
            expected_revision=created["revision"],
            limit=30,
            now=NOW,
        )
        assert resized["unit"] == "tries"
        assert resized["currency"] is None
        assert resized["limit"] == Decimal("30")
        closed = await transaction.update_budget(
            budget_id="budget-1",
            expected_revision=resized["revision"],
            status="closed",
            now=NOW,
        )
        assert closed["status"] == "closed"
        assert closed["unit"] == "tries"
        with pytest.raises(BudgetError, match="closed"):
            await transaction.update_budget(
                budget_id="budget-1",
                expected_revision=closed["revision"],
                limit=40,
                now=NOW,
            )


async def test_concurrent_reservations_cannot_overspend() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        await transaction.create_budget(
            budget_id="budget-1",
            scope="company",
            limit="10",
            currency="USD",
            now=NOW,
        )

    async def reserve(reservation_id: str) -> str:
        try:
            async with store.transaction("company-a") as transaction:
                await transaction.reserve_budget(
                    budget_id="budget-1",
                    reservation_id=reservation_id,
                    amount="7",
                    purpose="test",
                    now=NOW,
                )
            return "reserved"
        except BudgetError:
            return "rejected"

    outcomes = await asyncio.gather(reserve("one"), reserve("two"))
    assert sorted(outcomes) == ["rejected", "reserved"]
    exported = await store.export_company("company-a")
    assert exported["budgets"][0]["reserved"] == Decimal("7")


async def test_hierarchical_reservation_group_is_one_atomic_mirrored_hold() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        company = await transaction.create_budget(
            budget_id="company-budget",
            scope="company",
            limit="100",
            currency="USD",
            starts_at=NOW,
            ends_at=NOW + timedelta(days=2),
            now=NOW,
        )
        stage = await transaction.create_budget(
            budget_id="exploration-budget",
            scope="stage",
            parent_budget_id=company["budget_id"],
            stage="exploration",
            limit="20",
            currency="USD",
            starts_at=NOW,
            ends_at=NOW + timedelta(days=2),
            now=NOW,
        )
        cell = await transaction.create_budget(
            budget_id="cell-budget",
            scope="cell",
            parent_budget_id=company["budget_id"],
            cell_id="cell-1",
            limit="50",
            currency="USD",
            starts_at=NOW,
            ends_at=NOW + timedelta(days=1),
            now=NOW,
        )
        group = await transaction.reserve_budget_group(
            reservation_group_id="experiment-hold",
            budget_ids={
                "company": company["budget_id"],
                "cell": cell["budget_id"],
                "stage": stage["budget_id"],
            },
            amount="10",
            purpose="bounded experiment",
            expected_revisions={
                company["budget_id"]: 0,
                cell["budget_id"]: 0,
                stage["budget_id"]: 0,
            },
            idempotency_key="start-experiment",
            now=NOW,
        )
        retry = await transaction.reserve_budget_group(
            reservation_group_id="experiment-hold",
            budget_ids={
                "company": company["budget_id"],
                "cell": cell["budget_id"],
                "stage": stage["budget_id"],
            },
            amount="10",
            purpose="bounded experiment",
            expected_revisions={
                company["budget_id"]: 0,
                cell["budget_id"]: 0,
                stage["budget_id"]: 0,
            },
            idempotency_key="start-experiment",
            now=NOW,
        )
        assert retry == group
        assert group["amount"] == Decimal("10")
        assert set(group["reservation_ids"]) == {"company", "cell", "stage"}
        assert len(group["members"]) == 3
        assert all(member["amount"] == Decimal("10") for member in group["members"])

        committed = await transaction.commit_reservation_group(
            reservation_group_id="experiment-hold",
            actual_amount="4",
            expected_revisions={
                member["reservation_id"]: member["revision"] for member in group["members"]
            },
            now=NOW,
        )
        after_commit = await transaction.list_budgets(limit=10)
        release_candidate = await transaction.reserve_budget_group(
            reservation_group_id="release-hold",
            budget_ids={
                "company": company["budget_id"],
                "cell": cell["budget_id"],
                "stage": stage["budget_id"],
            },
            amount="2",
            purpose="cancelled experiment",
            expected_revisions={budget["budget_id"]: budget["revision"] for budget in after_commit},
            now=NOW,
        )
        released = await transaction.release_reservation_group(
            reservation_group_id="release-hold",
            expected_revisions={
                member["reservation_id"]: member["revision"]
                for member in release_candidate["members"]
            },
            now=NOW,
        )
        budgets = await transaction.list_budgets(limit=10)

    assert committed["status"] == "committed"
    assert committed["actual_amount"] == Decimal("4")
    assert released["status"] == "released"
    assert all(budget["spent"] == Decimal("4") for budget in budgets)
    assert all(budget["reserved"] == Decimal("0") for budget in budgets)


async def test_reservation_group_can_apply_incremental_spend_before_commit() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        company = await transaction.create_budget(
            budget_id="company-budget",
            scope="company",
            limit="100",
            currency="USD",
            now=NOW,
        )
        stage = await transaction.create_budget(
            budget_id="stage-budget",
            scope="stage",
            parent_budget_id=company["budget_id"],
            stage="exploration",
            limit="40",
            currency="USD",
            now=NOW,
        )
        cell = await transaction.create_budget(
            budget_id="cell-budget",
            scope="cell",
            parent_budget_id=company["budget_id"],
            cell_id="cell-1",
            limit="50",
            currency="USD",
            now=NOW,
        )
        group = await transaction.reserve_budget_group(
            reservation_group_id="try-hold",
            budget_ids={
                "company": company["budget_id"],
                "cell": cell["budget_id"],
                "stage": stage["budget_id"],
            },
            amount="10",
            purpose="running experiment",
            expected_revisions={
                company["budget_id"]: 0,
                cell["budget_id"]: 0,
                stage["budget_id"]: 0,
            },
            now=NOW,
        )
        spent = await transaction.apply_reservation_group_spend(
            reservation_group_id="try-hold",
            amount="3",
            expected_revisions={
                member["reservation_id"]: member["revision"] for member in group["members"]
            },
            now=NOW,
        )
        mid = await transaction.list_budgets(limit=10)
        committed = await transaction.commit_reservation_group(
            reservation_group_id="try-hold",
            actual_amount="3",
            expected_revisions={
                member["reservation_id"]: member["revision"] for member in spent["members"]
            },
            now=NOW,
        )
        budgets = await transaction.list_budgets(limit=10)

    assert spent["status"] == "active"
    assert spent["actual_amount"] == Decimal("3")
    assert all(budget["spent"] == Decimal("3") for budget in mid)
    assert all(budget["reserved"] == Decimal("7") for budget in mid)
    assert committed["status"] == "committed"
    assert committed["actual_amount"] == Decimal("3")
    assert all(budget["spent"] == Decimal("3") for budget in budgets)
    assert all(budget["reserved"] == Decimal("0") for budget in budgets)


async def test_reservation_group_can_resize_hold_without_changing_spend() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        company = await transaction.create_budget(
            budget_id="company-budget",
            scope="company",
            limit="100",
            currency="USD",
            now=NOW,
        )
        stage = await transaction.create_budget(
            budget_id="stage-budget",
            scope="stage",
            parent_budget_id=company["budget_id"],
            stage="exploration",
            limit="40",
            currency="USD",
            now=NOW,
        )
        cell = await transaction.create_budget(
            budget_id="cell-budget",
            scope="cell",
            parent_budget_id=company["budget_id"],
            cell_id="cell-1",
            limit="50",
            currency="USD",
            now=NOW,
        )
        group = await transaction.reserve_budget_group(
            reservation_group_id="try-hold",
            budget_ids={
                "company": company["budget_id"],
                "cell": cell["budget_id"],
                "stage": stage["budget_id"],
            },
            amount="10",
            purpose="running experiment",
            now=NOW,
        )
        spent = await transaction.apply_reservation_group_spend(
            reservation_group_id="try-hold",
            amount="3",
            expected_revisions={
                member["reservation_id"]: member["revision"] for member in group["members"]
            },
            now=NOW,
        )
        grown = await transaction.adjust_reservation_group_hold(
            reservation_group_id="try-hold",
            amount="16",
            expected_revisions={
                member["reservation_id"]: member["revision"] for member in spent["members"]
            },
            metadata={"amount_minor": 16},
            now=NOW,
        )
        grown_budgets = await transaction.list_budgets(limit=10)
        shrunk = await transaction.adjust_reservation_group_hold(
            reservation_group_id="try-hold",
            amount="3",
            expected_revisions={
                member["reservation_id"]: member["revision"] for member in grown["members"]
            },
            metadata={"amount_minor": 3},
            now=NOW,
        )
        shrunk_budgets = await transaction.list_budgets(limit=10)
        with pytest.raises(BudgetError, match="cannot drop below already applied spend"):
            await transaction.adjust_reservation_group_hold(
                reservation_group_id="try-hold",
                amount="2",
                now=NOW,
            )

    assert grown["status"] == "active"
    assert grown["amount"] == Decimal("16")
    assert grown["actual_amount"] == Decimal("3")
    assert all(budget["spent"] == Decimal("3") for budget in grown_budgets)
    assert all(budget["reserved"] == Decimal("13") for budget in grown_budgets)
    assert shrunk["amount"] == Decimal("3")
    assert shrunk["actual_amount"] == Decimal("3")
    assert all(budget["reserved"] == Decimal("0") for budget in shrunk_budgets)
    assert all(budget["spent"] == Decimal("3") for budget in shrunk_budgets)


async def test_hierarchical_group_rolls_back_if_any_ledger_is_insufficient() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        company = await transaction.create_budget(
            budget_id="company-budget",
            scope="company",
            limit="100",
            currency="USD",
            now=NOW,
        )
        await transaction.create_budget(
            budget_id="stage-budget",
            scope="stage",
            parent_budget_id=company["budget_id"],
            stage="exploration",
            limit="5",
            currency="USD",
            now=NOW,
        )
        await transaction.create_budget(
            budget_id="cell-budget",
            scope="cell",
            parent_budget_id=company["budget_id"],
            cell_id="cell-1",
            limit="50",
            currency="USD",
            now=NOW,
        )
        with pytest.raises(BudgetError, match="stage reservation"):
            await transaction.reserve_budget_group(
                reservation_group_id="too-large",
                budget_ids={
                    "company": "company-budget",
                    "cell": "cell-budget",
                    "stage": "stage-budget",
                },
                amount="6",
                purpose="must roll back",
                now=NOW,
            )
        budgets = await transaction.list_budgets(limit=10)
        assert all(budget["reserved"] == Decimal("0") for budget in budgets)
        assert await transaction.get_reservation_group("too-large") is None


async def test_concurrent_hierarchical_holds_cannot_oversubscribe_shared_ledgers() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        company = await transaction.create_budget(
            budget_id="company-budget",
            scope="company",
            limit="10",
            currency="USD",
            now=NOW,
        )
        await transaction.create_budget(
            budget_id="stage-budget",
            scope="stage",
            parent_budget_id=company["budget_id"],
            stage="exploitation",
            limit="10",
            currency="USD",
            now=NOW,
        )
        for cell_number in (1, 2):
            await transaction.create_budget(
                budget_id=f"cell-{cell_number}-budget",
                scope="cell",
                parent_budget_id=company["budget_id"],
                cell_id=f"cell-{cell_number}",
                limit="10",
                currency="USD",
                now=NOW,
            )

    async def reserve(cell_number: int) -> str:
        try:
            async with store.transaction("company-a") as transaction:
                await transaction.reserve_budget_group(
                    reservation_group_id=f"allocation-{cell_number}",
                    budget_ids={
                        "company": "company-budget",
                        "stage": "stage-budget",
                        "cell": f"cell-{cell_number}-budget",
                    },
                    amount="7",
                    purpose="portfolio allocation",
                    now=NOW,
                )
            return "reserved"
        except BudgetError:
            return "rejected"

    outcomes = await asyncio.gather(reserve(1), reserve(2))
    assert sorted(outcomes) == ["rejected", "reserved"]
    exported = await store.export_company("company-a")
    by_id = {budget["budget_id"]: budget for budget in exported["budgets"]}
    assert by_id["company-budget"]["reserved"] == Decimal("7")
    assert by_id["stage-budget"]["reserved"] == Decimal("7")
    assert len(exported["reservations"]) == 3


async def test_budget_failures_do_not_partially_mutate_state() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        await transaction.create_budget(
            budget_id="budget-1",
            scope="company",
            limit="10",
            currency="USD",
            now=NOW,
        )
    with pytest.raises(BudgetError):
        async with store.transaction("company-a") as transaction:
            await transaction.reserve_budget(
                budget_id="budget-1",
                reservation_id="bad",
                amount="11",
                purpose="too much",
                now=NOW,
            )
    exported = await store.export_company("company-a")
    assert exported["budgets"][0]["revision"] == 0
    assert exported["reservations"] == []


async def test_lease_exclusion_tokens_renewal_release_and_expiry() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        lease = await transaction.acquire_lease(
            work_item_id="work-1",
            agent_id="agent-1",
            agent_run_id="run-1",
            cell_id="cell-1",
            ttl=60,
            lease_id="lease-1",
            token="secret-token",
            now=NOW,
        )
        retry = await transaction.acquire_lease(
            work_item_id="work-1",
            agent_id="agent-1",
            agent_run_id="run-1",
            cell_id="cell-1",
            ttl=60,
            lease_id="lease-1",
            token="different-ignored-on-retry",
            now=NOW,
        )
        assert retry == lease
        assert lease["agent_run_id"] == "run-1"
        with pytest.raises(LeaseError, match="active lease"):
            await transaction.acquire_lease(
                work_item_id="work-1",
                agent_id="agent-1",
                agent_run_id="run-spoofed",
                cell_id="cell-1",
                ttl=60,
                lease_id="lease-1",
                token="secret-token",
                now=NOW,
            )
        with pytest.raises(LeaseError, match="active lease"):
            await transaction.acquire_lease(
                work_item_id="work-1",
                agent_id="agent-2",
                ttl=60,
                now=NOW,
            )
        with pytest.raises(LeaseError, match="token"):
            await transaction.renew_lease(
                work_item_id="work-1",
                token="wrong",
                ttl=120,
                now=NOW,
            )
        renewed = await transaction.renew_lease(
            work_item_id="work-1",
            token="secret-token",
            ttl=120,
            expected_revision=0,
            now=NOW,
        )
        released = await transaction.release_lease(
            work_item_id="work-1",
            token="secret-token",
            expected_revision=1,
            now=NOW,
        )
        assert renewed["revision"] == 1
        assert released["status"] == "released"
        assert await transaction.get_lease("work-1") is None

        replacement = await transaction.acquire_lease(
            work_item_id="work-1",
            agent_id="agent-2",
            agent_run_id="run-2",
            ttl=60,
            lease_id="lease-2",
            token="second-token",
            expected_revision=2,
            now=NOW,
        )
        assert replacement["revision"] == 3
        assert replacement["cell_id"] is None
        assert replacement["agent_run_id"] == "run-2"

    async with store.transaction("company-a") as transaction:
        assert await transaction.get_lease("work-1", now=NOW + timedelta(minutes=2)) is None
        expired_replacement = await transaction.acquire_lease(
            work_item_id="work-1",
            agent_id="agent-3",
            agent_run_id="run-3",
            ttl=60,
            now=NOW + timedelta(minutes=2),
        )
    assert expired_replacement["agent_id"] == "agent-3"
    exported = await store.export_company("company-a")
    assert "token" not in exported["work_leases"][0]
    assert exported["work_leases"][0]["agent_run_id"] == "run-3"


async def test_validation_and_not_found_errors_are_explicit() -> None:
    store = InMemoryStore()
    with pytest.raises(ValidationError, match="company_id"):
        async with store.transaction(" "):
            pass
    async with store.transaction("company-a") as transaction:
        with pytest.raises(NotFoundError, match="budget"):
            await transaction.reserve_budget(
                budget_id="missing",
                reservation_id="reservation",
                amount="1",
                purpose="test",
                now=NOW,
            )
        with pytest.raises(ValidationError, match="Decimal"):
            await transaction.create_budget(
                budget_id="budget",
                scope="company",
                limit=1.5,
                currency="USD",
                now=NOW,
            )


async def test_audit_retry_is_idempotent_but_collision_is_rejected() -> None:
    store = InMemoryStore()
    fields = {
        "audit_id": "audit-1",
        "action": "message.send",
        "outcome": "succeeded",
        "actor_id": "agent-1",
        "resource_type": "message",
        "resource_id": "message-1",
        "details": {"status": 200},
        "occurred_at": NOW,
    }
    async with store.transaction("company-a") as transaction:
        first = await transaction.append_audit_event(**fields)
        retry = await transaction.append_audit_event(**fields)
        assert retry == first
        with pytest.raises(ConflictError, match="another event"):
            await transaction.append_audit_event(**{**fields, "outcome": "failed"})


async def test_failed_idempotency_outcome_is_replayable_and_immutable() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        with pytest.raises(NotFoundError, match="claim"):
            await transaction.fail_idempotency(
                namespace="action",
                key="missing",
                request_hash="hash",
                error={"code": "failed"},
                now=NOW,
            )
        await transaction.begin_idempotency(
            namespace="action",
            key="key",
            request_hash="hash",
            now=NOW,
        )
        failed = await transaction.fail_idempotency(
            namespace="action",
            key="key",
            request_hash="hash",
            error={"code": "connector_failed"},
            now=NOW,
        )
        retry = await transaction.fail_idempotency(
            namespace="action",
            key="key",
            request_hash="hash",
            error={"code": "connector_failed"},
            now=NOW,
        )
        assert retry == failed
        with pytest.raises(ConflictError, match="already failed"):
            await transaction.complete_idempotency(
                namespace="action",
                key="key",
                request_hash="hash",
                response={"ok": True},
                now=NOW,
            )


async def test_budget_revision_lifecycle_and_time_guards() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        await transaction.create_budget(
            budget_id="future",
            scope="company",
            limit="10",
            currency="USD",
            starts_at=NOW + timedelta(hours=1),
            ends_at=NOW + timedelta(hours=2),
            now=NOW,
        )
        with pytest.raises(BudgetError, match="not started"):
            await transaction.reserve_budget(
                budget_id="future",
                reservation_id="future-reservation",
                amount="1",
                purpose="test",
                now=NOW,
            )
        await transaction.create_budget(
            budget_id="active",
            scope="company",
            limit="10",
            currency="USD",
            starts_at=NOW,
            ends_at=NOW + timedelta(hours=1),
            now=NOW,
        )
        with pytest.raises(ConflictError, match="stale budget"):
            await transaction.reserve_budget(
                budget_id="active",
                reservation_id="stale",
                amount="1",
                purpose="test",
                expected_revision=1,
                now=NOW,
            )
        reservation = await transaction.reserve_budget(
            budget_id="active",
            reservation_id="reservation",
            amount="5",
            purpose="test",
            expected_revision=0,
            now=NOW,
        )
        with pytest.raises(ConflictError, match="already exists"):
            await transaction.reserve_budget(
                budget_id="active",
                reservation_id="reservation",
                amount="4",
                purpose="different",
                now=NOW,
            )
        with pytest.raises(BudgetError, match="reserved amount"):
            await transaction.commit_reservation(
                reservation_id="reservation",
                actual_amount="6",
                now=NOW,
            )
        committed = await transaction.commit_reservation(
            reservation_id="reservation",
            actual_amount="5",
            expected_revision=reservation["revision"],
            now=NOW,
        )
        assert (
            await transaction.commit_reservation(
                reservation_id="reservation",
                actual_amount="5",
                now=NOW,
            )
            == committed
        )
        with pytest.raises(ConflictError, match="cannot release"):
            await transaction.release_reservation(reservation_id="reservation", now=NOW)


async def test_lease_stale_expired_and_missing_paths_are_safe() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        with pytest.raises(NotFoundError, match="does not exist"):
            await transaction.renew_lease(
                work_item_id="missing",
                token="token",
                ttl=10,
                now=NOW,
            )
        lease = await transaction.acquire_lease(
            work_item_id="work",
            agent_id="agent",
            ttl=10,
            token="token",
            now=NOW,
        )
        with pytest.raises(ConflictError, match="stale lease"):
            await transaction.renew_lease(
                work_item_id="work",
                token="token",
                ttl=20,
                expected_revision=lease["revision"] + 1,
                now=NOW,
            )
        with pytest.raises(LeaseError, match="extend"):
            await transaction.renew_lease(
                work_item_id="work",
                token="token",
                ttl=5,
                expected_revision=lease["revision"],
                now=NOW,
            )
        with pytest.raises(LeaseError, match="expired"):
            await transaction.release_lease(
                work_item_id="work",
                token="token",
                now=NOW + timedelta(seconds=10),
            )
        with pytest.raises(ValidationError, match="duration"):
            await transaction.acquire_lease(
                work_item_id="boolean-ttl",
                agent_id="agent",
                ttl=True,
                now=NOW,
            )


async def test_expired_lease_can_be_reconciled_and_replaced_with_a_fresh_secret() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        original = await transaction.acquire_lease(
            work_item_id="work",
            agent_id="agent-1",
            agent_run_id="run-1",
            ttl=10,
            token="old-token",
            now=NOW,
        )
        with pytest.raises(LeaseError, match="not expired"):
            await transaction.expire_lease(
                work_item_id="work",
                expected_revision=original["revision"],
                now=NOW + timedelta(seconds=9),
            )

        expired = await transaction.expire_lease(
            work_item_id="work",
            expected_revision=original["revision"],
            now=NOW + timedelta(seconds=10),
        )
        assert expired["status"] == "released"
        assert expired["released_at"] == NOW + timedelta(seconds=10)
        assert await transaction.get_lease("work", now=NOW + timedelta(seconds=10)) is None

        replacement = await transaction.acquire_lease(
            work_item_id="work",
            agent_id="agent-2",
            agent_run_id="run-2",
            ttl=10,
            expected_revision=expired["revision"],
            now=NOW + timedelta(seconds=10),
        )
        assert replacement["status"] == "active"
        assert replacement["token"] != original["token"]
        assert len(replacement["token"]) >= 32


async def test_bounded_queries_and_execute_helper_validate_inputs() -> None:
    store = InMemoryStore()

    async def operation(transaction: StoreTransaction) -> str:
        assert transaction.company_id == "company-a"
        return "done"

    assert await store.execute("company-a", operation) == "done"
    async with store.transaction("company-a") as transaction:
        with pytest.raises(ValidationError, match="limit"):
            await transaction.list_events(limit=0)
        with pytest.raises(ValidationError, match="after_position"):
            await transaction.list_audit_events(after_position=True)
        with pytest.raises(ValidationError, match="expected_revision"):
            await transaction.put_projection(
                projection_type="offer",
                projection_id="offer",
                state={},
                expected_revision=True,
            )
        with pytest.raises(ValidationError, match="UTC offset"):
            await transaction.append_event(
                aggregate_type="offer",
                aggregate_id="offer",
                event_type="offer.created",
                actor_id="agent",
                payload={},
                occurred_at=datetime(2026, 8, 29),
            )


async def test_time_and_tries_ledgers_reserve_and_reject_mixed_units() -> None:
    store = InMemoryStore()
    async with store.transaction("company-a") as transaction:
        time_company = await transaction.create_budget(
            budget_id="time-company",
            scope="company",
            limit=3_600,
            unit="time",
            now=NOW,
        )
        await transaction.create_budget(
            budget_id="time-cell",
            scope="cell",
            parent_budget_id=time_company["budget_id"],
            cell_id="cell-1",
            limit=1_800,
            unit="time",
            now=NOW,
        )
        assert time_company["unit"] == "time"
        assert time_company["currency"] is None
        assert time_company["limit"] == Decimal(3_600)
        reserved = await transaction.reserve_budget(
            budget_id="time-cell",
            reservation_id="time-hold",
            amount=600,
            purpose="attention",
            now=NOW,
        )
        assert reserved["unit"] == "time"
        assert reserved["amount"] == Decimal(600)
        with pytest.raises(BudgetError, match="available budget"):
            await transaction.reserve_budget(
                budget_id="time-cell",
                reservation_id="time-overspend",
                amount=1_201,
                purpose="attention",
                now=NOW,
            )
        with pytest.raises(ValidationError, match="unit of account"):
            await transaction.create_budget(
                budget_id="usd-child",
                scope="cell",
                parent_budget_id=time_company["budget_id"],
                cell_id="cell-2",
                limit="10.00",
                currency="USD",
                now=NOW,
            )
        tries = await transaction.create_budget(
            budget_id="tries-company",
            scope="company",
            limit=20,
            unit="tries",
            now=NOW,
        )
        tries_cell = await transaction.create_budget(
            budget_id="tries-cell",
            scope="cell",
            parent_budget_id=tries["budget_id"],
            cell_id="cell-3",
            limit=10,
            unit="tries",
            now=NOW,
        )
        tries_stage = await transaction.create_budget(
            budget_id="tries-stage",
            scope="stage",
            parent_budget_id=tries["budget_id"],
            stage="exploration",
            limit=8,
            unit="tries",
            now=NOW,
        )
        with pytest.raises(BudgetError, match="unit of account"):
            await transaction.reserve_budget_group(
                reservation_group_id="mixed-hold",
                budget_ids={
                    "company": time_company["budget_id"],
                    "cell": tries_cell["budget_id"],
                    "stage": tries_stage["budget_id"],
                },
                amount=2,
                purpose="mixed",
                now=NOW,
            )
        group = await transaction.reserve_budget_group(
            reservation_group_id="tries-hold",
            budget_ids={
                "company": tries["budget_id"],
                "cell": tries_cell["budget_id"],
                "stage": tries_stage["budget_id"],
            },
            amount=3,
            purpose="attempts",
            now=NOW,
        )
        assert group["unit"] == "tries"
        assert group["amount"] == Decimal(3)
