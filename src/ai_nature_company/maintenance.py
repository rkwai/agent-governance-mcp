"""On-demand Maintain/Continue scan: findings plus "would do".

Nothing schedules this module.  State is kept accurate by construction (for
example, killing a cell ends its agent runs in the same transaction); the scan
is an explicitly invoked check, exposed as the ``maintenance.run`` tool for a
founder or a cell owner.  Findings are built through
:class:`ReadOnlyTransaction`, which refuses every write method, so a report
never changes domain state.  The only optional repair, ending runs that are
still running on a killed cell, happens in the tool handler outside the
read-only wrapper, behind an idempotency key and an audit row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .errors import ValidationError

# v2 adds ``scanned_by_cell`` (per-cell scan counts used for cell-scoped
# views).  Readers still accept v1 reports, which lack it.
MAINTENANCE_REPORT_VERSION = 2
RUN_EXPIRY_WARNING_WINDOW = timedelta(hours=72)
MAX_REPORT_FINDINGS = 200
# Every scan pages to exhaustion.  A safety cap bounds one report's work; when a
# cap is reached the section says ``truncated: true`` with counts, never
# ``complete``.
DEFAULT_PROJECTION_SCAN_CAP = 50_000
PROJECTION_SCAN_PAGE_SIZE = 1_000
DEFAULT_AGENT_RUN_SCAN_CAP = 10_000
AGENT_RUN_SCAN_PAGE_SIZE = 200

_OPEN_EXPERIMENT_STATES = frozenset({"registered", "running", "evaluating"})
_OPEN_WORK_STATES = frozenset({"open", "claimed"})
_TERMINAL_CELL_STATES = frozenset({"killed"})

_READ_METHODS = frozenset(
    {
        "get_projection",
        "list_projections",
        "get_lease",
        "get_budget",
        "list_budgets",
        "get_reservation",
        "get_reservation_group",
        "get_idempotency",
        "list_events",
        "list_audit_events",
        "list_running_agent_runs",
        "export",
        "company_id",
    }
)


class MaintenanceMutationRefused(RuntimeError):
    """A report-mode code path tried to write domain state."""


class ReadOnlyTransaction:
    """Expose only read methods of a store transaction.

    Any attribute outside the read allowlist raises, so a report builder that
    accidentally reuses a mutating helper fails loudly instead of writing.
    """

    __slots__ = ("_tx",)

    def __init__(self, tx: Any) -> None:
        self._tx = tx

    def __getattr__(self, name: str) -> Any:
        if name in _READ_METHODS:
            return getattr(self._tx, name)
        raise MaintenanceMutationRefused(
            f"maintenance scan refused {name!r}: the scan makes zero state mutations"
        )


@dataclass(frozen=True, slots=True)
class MaintenanceAgentRun:
    """One running agent run, content-minimized (no subjects, no secrets)."""

    agent_run_id: str
    agent_instance_id: str
    role: str
    cell_id: str | None
    expires_at: datetime
    instance_status: str = "active"


@dataclass(frozen=True, slots=True)
class MaintenanceAgentRunScan:
    """All running runs a source returned, and whether its safety cap cut it short."""

    runs: tuple[MaintenanceAgentRun, ...]
    truncated: bool = False
    cap: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectionScan:
    records: tuple[Mapping[str, Any], ...]
    truncated: bool
    cap: int


async def scan_projections(
    tx: Any,
    projection_type: str,
    *,
    cap: int = DEFAULT_PROJECTION_SCAN_CAP,
    page_size: int = PROJECTION_SCAN_PAGE_SIZE,
) -> ProjectionScan:
    """Keyset-page one projection type to exhaustion, stopping only at ``cap``."""

    if cap < 1 or page_size < 1:
        raise ValidationError("scan cap and page size must be positive")
    records: list[Mapping[str, Any]] = []
    after: str | None = None
    while True:
        remaining = cap - len(records)
        if remaining <= 0:
            probe = await tx.list_projections(
                projection_type=projection_type, after_projection_id=after, limit=1
            )
            return ProjectionScan(tuple(records), bool(probe), cap)
        want = min(page_size, remaining)
        page = await tx.list_projections(
            projection_type=projection_type, after_projection_id=after, limit=want
        )
        records.extend(page)
        if len(page) < want:
            return ProjectionScan(tuple(records), False, cap)
        after = str(page[-1]["projection_id"])


async def scan_running_agent_runs(
    tx: Any,
    *,
    cap: int = DEFAULT_AGENT_RUN_SCAN_CAP,
    page_size: int = AGENT_RUN_SCAN_PAGE_SIZE,
) -> MaintenanceAgentRunScan:
    """Keyset-page running runs on (expires_at, agent_run_id) to exhaustion or ``cap``."""

    if cap < 1 or page_size < 1:
        raise ValidationError("scan cap and page size must be positive")
    runs: list[MaintenanceAgentRun] = []
    after_expires: datetime | None = None
    after_id: str | None = None
    while True:
        remaining = cap - len(runs)
        want = max(1, min(page_size, remaining))
        rows = await tx.list_running_agent_runs(
            after_expires_at=after_expires, after_agent_run_id=after_id, limit=want
        )
        if remaining <= 0:
            return MaintenanceAgentRunScan(tuple(runs), truncated=bool(rows), cap=cap)
        for row in rows:
            expires_at = row.get("expires_at")
            if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
                continue
            runs.append(
                MaintenanceAgentRun(
                    agent_run_id=str(row["agent_run_id"]),
                    agent_instance_id=str(row["agent_instance_id"]),
                    role=str(row["role"]),
                    cell_id=str(row["cell_id"]) if row.get("cell_id") else None,
                    expires_at=expires_at.astimezone(UTC),
                    instance_status=str(row.get("instance_status") or "active"),
                )
            )
        if len(rows) < want:
            return MaintenanceAgentRunScan(tuple(runs), truncated=False, cap=cap)
        after_expires, after_id = rows[-1]["expires_at"], str(rows[-1]["agent_run_id"])


def section_status(*, scanned: int, truncated: bool, cap: int | None) -> dict[str, Any]:
    """Report section marker: ``complete`` only when nothing was cut off."""

    return {
        "status": "truncated" if truncated else "complete",
        "truncated": truncated,
        "scanned": scanned,
        "cap": cap,
    }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


def _finding(
    kind: str,
    severity: str,
    *,
    resource_type: str,
    resource_id: str,
    cell_id: str | None,
    would_do: str | None,
    observed: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "severity": severity,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "cell_id": cell_id,
        "would_do": would_do,
        "observed": dict(observed),
    }


def agent_run_findings(
    runs: Sequence[MaintenanceAgentRun],
    *,
    now: datetime,
    cell_status: Mapping[str, str],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for run in runs:
        remaining = run.expires_at - now
        observed = {
            "agent_instance_id": run.agent_instance_id,
            "role": run.role,
            "expires_at": _iso(run.expires_at),
            "hours_remaining": round(remaining.total_seconds() / 3600, 1),
        }
        if remaining <= timedelta(0):
            findings.append(
                _finding(
                    "agent_run_expired",
                    "critical",
                    resource_type="agent_run",
                    resource_id=run.agent_run_id,
                    cell_id=run.cell_id,
                    would_do="rotate_agent_run",
                    observed=observed,
                )
            )
        elif remaining <= RUN_EXPIRY_WARNING_WINDOW:
            findings.append(
                _finding(
                    "agent_run_expiring",
                    "warning",
                    resource_type="agent_run",
                    resource_id=run.agent_run_id,
                    cell_id=run.cell_id,
                    would_do="rotate_agent_run",
                    observed=observed,
                )
            )
        if run.cell_id is not None and cell_status.get(run.cell_id) in _TERMINAL_CELL_STATES:
            findings.append(
                _finding(
                    "agent_run_on_killed_cell",
                    "info",
                    resource_type="agent_run",
                    resource_id=run.agent_run_id,
                    cell_id=run.cell_id,
                    would_do="end_agent_run",
                    observed={**observed, "cell_status": cell_status[run.cell_id]},
                )
            )
    return findings


async def work_findings(
    tx: ReadOnlyTransaction,
    work_items: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for state in work_items:
        status = state.get("status")
        work_id = str(state["work_item_id"])
        cell_id = str(state["cell_id"]) if state.get("cell_id") else None
        deadline = _as_datetime(state.get("deadline_at"))
        base = {"kind": state.get("kind"), "status": status}
        if deadline is not None:
            base["deadline_at"] = _iso(deadline)
        if status == "open":
            if deadline is not None and deadline <= now:
                findings.append(
                    _finding(
                        "open_work_deadline_elapsed",
                        "warning",
                        resource_type="work_item",
                        resource_id=work_id,
                        cell_id=cell_id,
                        would_do="fail_work_deadline_elapsed",
                        observed=base,
                    )
                )
            continue
        if status != "claimed":
            continue
        lease = await tx.get_lease(work_id, include_inactive=True, now=now)
        runtime = await tx.get_projection("work_runtime", work_id)
        attempt = int((runtime or {}).get("state", {}).get("attempt", 0))
        max_attempts = int(state.get("max_attempts", 1))
        observed = {**base, "attempt": attempt, "max_attempts": max_attempts}
        lease_expires = _as_datetime(lease.get("expires_at")) if lease is not None else None
        if lease is not None and lease.get("status") == "active" and lease_expires is not None:
            observed["lease_expires_at"] = _iso(lease_expires)
            if lease_expires <= now:
                findings.append(
                    _finding(
                        "work_lease_expired",
                        "warning",
                        resource_type="work_item",
                        resource_id=work_id,
                        cell_id=cell_id,
                        would_do=(
                            "expire_lease_and_fail_work"
                            if attempt >= max_attempts
                            # The mutating tick requeues first, then fails overdue
                            # open work in the same pass.
                            else "expire_lease_and_fail_work_deadline_elapsed"
                            if deadline is not None and deadline <= now
                            else "expire_lease_and_requeue_work"
                        ),
                        observed=observed,
                    )
                )
                continue
        else:
            findings.append(
                _finding(
                    "claimed_work_without_active_lease",
                    "warning",
                    resource_type="work_item",
                    resource_id=work_id,
                    cell_id=cell_id,
                    would_do="flag_cell_owner",
                    observed=observed,
                )
            )
            continue
        if deadline is not None and deadline <= now:
            findings.append(
                _finding(
                    "claimed_work_past_deadline",
                    "info",
                    resource_type="work_item",
                    resource_id=work_id,
                    cell_id=cell_id,
                    would_do="flag_cell_owner",
                    observed=observed,
                )
            )
    return findings


def experiment_findings(
    views: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]],
    *,
    now: datetime,
    closeout_work: Mapping[str, Mapping[str, Any] | None],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for state, view in views:
        experiment_id = str(state["experiment_id"])
        cell_id = str(state["cell_id"]) if state.get("cell_id") else None
        status = view.get("state")
        has_evaluation = bool(state.get("evaluation_id"))
        observed: dict[str, Any] = {
            "state": status,
            "window_ends_at": view.get("window_ends_at"),
            "stop_state": view.get("stop_state"),
            "has_evaluation": has_evaluation,
        }
        fired = [rule.get("rule_id") for rule in view.get("fired_rules") or ()]
        if fired:
            observed["fired_rules"] = fired
        if status == "running" and view.get("close_eligible"):
            findings.append(
                _finding(
                    "experiment_close_eligible",
                    "warning",
                    resource_type="experiment",
                    resource_id=experiment_id,
                    cell_id=cell_id,
                    would_do="close_experiment_and_open_closeout_evaluation_work",
                    observed=observed,
                )
            )
        elif status == "evaluating" and not has_evaluation:
            closed_at = _as_datetime(state.get("closed_at"))
            if closed_at is not None:
                observed["closed_at"] = _iso(closed_at)
                observed["days_since_close"] = round((now - closed_at).total_seconds() / 86400, 1)
            work = closeout_work.get(experiment_id)
            observed["closeout_work_status"] = work.get("status") if work else None
            findings.append(
                _finding(
                    "experiment_awaiting_evaluation",
                    "warning",
                    resource_type="experiment",
                    resource_id=experiment_id,
                    cell_id=cell_id,
                    would_do=(
                        "open_closeout_evaluation_work"
                        if work is None
                        else "flag_independent_evaluator"
                    ),
                    observed=observed,
                )
            )
    return findings


def idle_cell_findings(
    cells: Sequence[Mapping[str, Any]],
    *,
    open_work_by_cell: Mapping[str, int],
    open_experiments_by_cell: Mapping[str, int],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for cell in cells:
        if cell.get("status") != "active":
            continue
        cell_id = str(cell["cell_id"])
        if open_work_by_cell.get(cell_id, 0) or open_experiments_by_cell.get(cell_id, 0):
            continue
        findings.append(
            _finding(
                "active_cell_idle",
                "info",
                resource_type="cell",
                resource_id=cell_id,
                cell_id=cell_id,
                would_do="flag_cell_owner",
                observed={"status": "active", "open_work": 0, "open_experiments": 0},
            )
        )
    return findings


def summarize(findings: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["kind"]] = counts.get(finding["kind"], 0) + 1
    return dict(sorted(counts.items()))


def filter_report_for_cell(report: Mapping[str, Any], cell_id: str) -> dict[str, Any]:
    """Cell-scoped principals see only their own cell's findings."""

    findings = [item for item in report.get("findings", ()) if item.get("cell_id") == cell_id]
    filtered = dict(report)
    filtered["findings"] = findings
    by_cell = report.get("counts_by_cell")
    filtered["counts"] = (
        dict(by_cell.get(cell_id, {})) if isinstance(by_cell, Mapping) else summarize(findings)
    )
    if isinstance(by_cell, Mapping):
        filtered["counts_by_cell"] = {cell_id: dict(by_cell.get(cell_id, {}))}
    filtered["would_do"] = [
        item for item in report.get("would_do", ()) if item.get("cell_id") == cell_id
    ]
    # Aggregates must describe only this cell; company-wide totals and scan
    # counts would let a cell reader infer activity in other cells.
    filtered["total_findings"] = sum(filtered["counts"].values()) if by_cell else len(findings)
    filtered["findings_truncated"] = len(findings) < filtered["total_findings"]
    scanned_by_cell = report.get("scanned_by_cell")
    # v1 reports carry no per-cell breakdown: withhold scan counts (None)
    # rather than leak company totals or claim zero.
    has_breakdown = isinstance(scanned_by_cell, Mapping)
    own = dict(scanned_by_cell.get(cell_id, {})) if has_breakdown else {}
    filtered.pop("scanned_by_cell", None)
    filtered["scanned"] = {
        key: own.get(key, 0) if value is not None and has_breakdown else None
        for key, value in dict(report.get("scanned") or {}).items()
    }
    section_keys = {
        "agent_runs": "agent_runs",
        "work_leases": "work_items",
        "experiments": "experiments",
        "idle_cells": "cells",
    }
    sections: dict[str, Any] = {}
    for name, section in dict(report.get("sections") or {}).items():
        scoped = dict(section) if isinstance(section, Mapping) else section
        if isinstance(scoped, dict) and scoped.get("scanned") is not None:
            if name == "findings":
                scoped["scanned"] = filtered["total_findings"]
            else:
                scoped["scanned"] = (
                    own.get(section_keys.get(name, ""), 0) if has_breakdown else None
                )
        sections[name] = scoped
    filtered["sections"] = sections
    filtered["scoped_to_cell_id"] = cell_id
    return filtered


__all__ = [
    "DEFAULT_AGENT_RUN_SCAN_CAP",
    "DEFAULT_PROJECTION_SCAN_CAP",
    "MAINTENANCE_REPORT_VERSION",
    "MAX_REPORT_FINDINGS",
    "MaintenanceAgentRun",
    "MaintenanceAgentRunScan",
    "MaintenanceMutationRefused",
    "ProjectionScan",
    "ReadOnlyTransaction",
    "agent_run_findings",
    "experiment_findings",
    "filter_report_for_cell",
    "idle_cell_findings",
    "scan_projections",
    "scan_running_agent_runs",
    "section_status",
    "summarize",
    "work_findings",
]
