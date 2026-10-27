"""Deterministic application service consumed by MCP-hosted agents.

The service is the command boundary between authenticated agent principals and the
company ledger.  It contains no model inference, agent scheduler, or provider
credentials.  Every mutation is company-scoped, idempotent, revision-checked, and
recorded as both a domain event and a content-minimized audit event.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from .auth import (
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    ResourceType,
    Role,
    ScopedRoleGrant,
    VerifiedPrincipal,
    is_verified_principal,
)
from .bet_workflow import (
    CELL_RECORD_KINDS,
    MAX_CELL_RECORD_BYTES,
    MAX_CELL_RECORDS,
    describe_bet_workflow,
)
from .cast import CELL_ROUTINES, REFLEX_FORBIDDEN_CAPABILITIES, REFLEX_WORK_KINDS
from .domain.transitions import amend_constitution, transition_offer_cell
from .errors import (
    AuthorizationError,
    BudgetError,
    ConflictError,
    InternalError,
    LeaseError,
    NotFoundError,
    SeparationOfDutiesError,
    SubstrateError,
    ValidationError,
)
from .evidence_class import classify_evidence
from .experiment_stop import (
    cell_operating_eligibility,
    experiment_stop_assessment,
    parse_aware_datetime,
)
from .fitness import (
    FitnessThresholds,
    GateObservations,
    canonical_decision_digest,
    compute_exploitation_ranking,
    compute_exploration_ranking,
    containment_ranking,
    evaluate_fitness_gates,
    gates_are_eligible,
)
from .forces import describe_effectors, describe_natural_forces, work_kind_force
from .ledger_units import (
    MONEY,
    dump_budget_amount,
    identity_of,
    ledger_to_quantity,
    parse_limit_to_minor,
    quantity_to_ledger,
    same_ledger_identity,
)
from .maintenance import (
    DEFAULT_AGENT_RUN_SCAN_CAP,
    DEFAULT_PROJECTION_SCAN_CAP,
    MAINTENANCE_REPORT_VERSION,
    MAX_REPORT_FINDINGS,
    MaintenanceAgentRun,
    MaintenanceAgentRunScan,
    ReadOnlyTransaction,
    agent_run_findings,
    experiment_findings,
    filter_report_for_cell,
    idle_cell_findings,
    scan_projections,
    scan_running_agent_runs,
    section_status,
    summarize,
    work_findings,
)
from .models import (
    AgentDefinition,
    Allocation,
    AllocationAction,
    AllocationStage,
    AllocationStatus,
    ApprovalRequirement,
    AuthorityPolicy,
    BudgetPolicy,
    BudgetScope,
    CellStatus,
    Company,
    CompanyStatus,
    Constitution,
    ConstitutionStatus,
    Evaluation,
    EvaluationVerdict,
    EvidenceEnvelope,
    Experiment,
    ExperimentStatus,
    ExperimentWindow,
    FitnessPolicy,
    GateName,
    IndependenceAttestation,
    IndependenceBasis,
    LearningCandidate,
    LearningStatus,
    Offer,
    OfferCell,
    OutcomeContract,
    ResourceReference,
    RiskPolicy,
    StopRule,
    WorkBudget,
    WorkItem,
    WorkStatus,
)
from .policy import ROLE_CAPABILITIES, AuthorizationPolicy, Capability
from .store import InMemoryStore, Repository, StoreTransaction, _redact_export_secrets

Clock = Callable[[], datetime]
LOGGER = logging.getLogger(__name__)

CELL_KILLED_TERMINATION_CODE = "cell_killed"
KILL_EVENT_RUN_ID_LIMIT = 100
RUNTIME_TICK_PRINCIPAL_ID = str(uuid5(NAMESPACE_URL, "ai-nature-company:runtime.tick"))
CELL_OWNER_OPERATIONAL_TOOLS = frozenset(
    {
        "allocation.commit",
        "allocation.propose",
        "audit.list",
        "cell.amend",
        "cell.transition",
        "decision.explain",
        "experiment.amend",
        "experiment.record_send",
        "gate.evaluate",
        "maintenance.run",
        "work.cancel",
    }
)
_COMPANY_WIDE_AUDIT_ROLES = frozenset({Role.FOUNDER, Role.IMMUNE, Role.OBSERVER})
EVALUATOR_CLOSEOUT_TOOLS = frozenset(
    {
        "work.claim",
        "work.complete",
        "work.fail",
        "work.heartbeat",
        "work.release",
    }
)
_CONTAINMENT_BASIS_KINDS = frozenset({"founder_authorized", "cell_owner_authorized"})


def runtime_tick_principal(company_id: str) -> VerifiedPrincipal:
    """Issue the process-owned tick actor.  This is not a Grok bot or Auth user."""

    record = PrincipalRecord(
        principal_id=RUNTIME_TICK_PRINCIPAL_ID,
        grants=(ScopedRoleGrant(Role.OBSERVER, ResourceScope.company(company_id)),),
    )
    return LocalTestAuthenticator({"runtime-tick": record}).authenticate("runtime-tick")


def _expand_live_operating_tools(instance: Mapping[str, Any]) -> dict[str, Any]:
    """Union operating tools so existing closed-beta bindings keep working."""

    expanded = dict(instance)
    extra = frozenset()
    if expanded.get("role") == Role.CELL_OWNER.value:
        extra = CELL_OWNER_OPERATIONAL_TOOLS
    elif expanded.get("role") == Role.EVALUATOR.value:
        extra = EVALUATOR_CLOSEOUT_TOOLS
    if not extra:
        return expanded
    declared = set(expanded.get("capabilities", ()))
    allowed = set(expanded.get("allowed_actions", declared))
    declared |= extra
    allowed |= extra
    expanded["capabilities"] = sorted(declared)
    expanded["allowed_actions"] = sorted(allowed)
    return expanded


def _expand_cell_owner_operations(instance: Mapping[str, Any]) -> dict[str, Any]:
    return _expand_live_operating_tools(instance)


def _containment_basis_verified(
    stage: str,
    containment_basis: object,
    *,
    action: str | None = None,
    founder_principal_ids: set[str] | frozenset[str] | None = None,
) -> bool:
    if stage != AllocationStage.CONTAINMENT.value:
        return False
    if not isinstance(containment_basis, Mapping):
        return False
    kind = containment_basis.get("kind")
    if kind not in _CONTAINMENT_BASIS_KINDS:
        return False
    if kind == "cell_owner_authorized" and action == AllocationAction.KILL.value:
        return False
    if (
        kind == "founder_authorized"
        and founder_principal_ids is not None
        and containment_basis.get("authorized_by_principal_id") not in founder_principal_ids
    ):
        return False
    return bool(
        containment_basis.get("authorized_by_principal_id")
        and containment_basis.get("authorized_at")
    )


READ_TOOLS = frozenset(
    {
        "company.describe",
        "constitution.get",
        "protocol.describe",
        "agent.get",
        "agent.list",
        "cell.get",
        "cell.list",
        "audit.list",
        "decision.explain",
    }
)

MUTATION_TOOLS = frozenset(
    {
        "company.bootstrap",
        "constitution.amend",
        "agent.register",
        "work.create",
        "work.claim",
        "work.heartbeat",
        "work.complete",
        "work.fail",
        "work.release",
        "work.cancel",
        "cell.create",
        "cell.transition",
        "cell.amend",
        "offer.register",
        "offer.mutate",
        "experiment.register",
        "experiment.start",
        "experiment.close",
        "experiment.amend",
        "experiment.record_send",
        "evidence.append",
        "economics.append",
        "evaluation.submit",
        "gate.evaluate",
        "allocation.propose",
        "allocation.commit",
        "learning.propose",
        "learning.promote",
        "maintenance.run",
    }
)


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid(value: object, field: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError(f"{field} must be a UUID") from exc


def _required_str(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required")
    return value.strip()


def _optional_str(
    payload: Mapping[str, Any],
    field: str,
    *,
    max_length: int,
) -> str | None:
    if field not in payload:
        return None
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string when provided")
    stripped = value.strip()
    if len(stripped) > max_length:
        raise ValidationError(f"{field} is too long")
    return stripped


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValidationError(f"{field} must be a decimal string or integer")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:  # Decimal raises several implementation-specific errors.
        raise ValidationError(f"{field} must be a decimal amount") from exc
    if not result.is_finite():
        raise ValidationError(f"{field} must be finite")
    return result


def _minor_amount(value: object, field: str) -> int:
    """Read an exact non-negative minor-unit amount without accepting floats."""

    if isinstance(value, (bool, float)):
        raise ValidationError(f"{field} must be an integer")
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValidationError(f"{field} must be an integer") from exc
    if str(result) != str(value) and not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    if result < 0:
        raise ValidationError(f"{field} must not be negative")
    return result


def _is_stale_open_work(state: Mapping[str, Any], now: datetime) -> bool:
    if state.get("status") != WorkStatus.OPEN.value:
        return False
    deadline = state.get("deadline_at")
    if deadline is None:
        return False
    try:
        return parse_aware_datetime(deadline, "deadline_at") <= now
    except ValidationError:
        return False


def _budget_snapshot(budget: Mapping[str, Any]) -> dict[str, Any]:
    unit, currency = identity_of(budget)
    limit_minor = ledger_to_quantity(budget["limit"], unit, "budget.limit")
    spent_minor = ledger_to_quantity(budget["spent"], unit, "budget.spent")
    reserved_minor = ledger_to_quantity(budget["reserved"], unit, "budget.reserved")
    snapshot = {
        "budget_id": str(budget["budget_id"]),
        "scope": str(budget["scope"]),
        "stage": budget.get("stage"),
        "revision": int(budget["revision"]),
        "unit": unit,
        "limit_minor": limit_minor,
        "spent_minor": spent_minor,
        "reserved_minor": reserved_minor,
        "available_minor": limit_minor - spent_minor - reserved_minor,
    }
    if currency is not None:
        snapshot["currency"] = currency
    return snapshot


def _require_matching_unit(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    message: str,
) -> None:
    if not same_ledger_identity(left, right):
        raise ValidationError(message)


def _send_fingerprint(*, experiment_id: str, target: str, channel: str, sent_at: str) -> str:
    """Stable identity for one founder-signed external send.

    Same experiment + target + channel + sent_at is the same send even when the
    caller mints a new send_id. Case-folded so ``Acme`` and ``acme``
    cannot be double-counted.
    """

    return "|".join(
        (
            experiment_id.strip(),
            target.strip().casefold(),
            channel.strip().casefold(),
            sent_at,
        )
    )


def _message_cap_value(raw: object, field: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValidationError(f"{field} must be a non-negative integer")
    return raw


def _external_message_cap(constitution_state: Mapping[str, Any]) -> int:
    """Read the stored constitution send cap. Missing or invalid values fail closed."""

    risk = constitution_state.get("risk_policy")
    if not isinstance(risk, Mapping):
        raise ValidationError(
            "constitution risk_policy is required to enforce the external message cap"
        )
    return _message_cap_value(
        risk.get("max_external_messages_per_experiment"),
        "max_external_messages_per_experiment",
    )


def _experiment_message_override(experiment: Mapping[str, Any]) -> int | None:
    if "max_external_messages" not in experiment:
        return None
    raw = experiment.get("max_external_messages")
    if raw is None:
        return None
    return _message_cap_value(raw, "max_external_messages")


def _effective_message_cap(
    experiment: Mapping[str, Any],
    constitution_state: Mapping[str, Any],
) -> int:
    """Use the experiment override when present; otherwise the constitution cap."""

    override = _experiment_message_override(experiment)
    if override is not None:
        return override
    return _external_message_cap(constitution_state)


def _external_messages_remaining(*, cap: int, recorded_send_count: int) -> int:
    if recorded_send_count < 0:
        raise ValidationError("recorded_send_count must not be negative")
    return max(0, cap - recorded_send_count)


def _audit_event_cell_id(event: Mapping[str, Any]) -> str | None:
    details = event.get("details")
    if not isinstance(details, Mapping):
        return None
    raw = details.get("cell_id")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def _is_work_audit_event(event: Mapping[str, Any]) -> bool:
    action = event.get("action")
    resource_type = event.get("resource_type")
    return (isinstance(action, str) and action.startswith("work.")) or resource_type == "work_item"


def _resolved_audit_event_cell_id(
    event: Mapping[str, Any], work_item_cells: Mapping[str, str]
) -> str | None:
    """Prove a cell for audit rows. Work events may omit details.cell_id."""

    cell_id = _audit_event_cell_id(event)
    if cell_id is not None:
        return cell_id
    if not _is_work_audit_event(event):
        return None
    resource_id = event.get("resource_id")
    if not isinstance(resource_id, str) or not resource_id.strip():
        return None
    return work_item_cells.get(resource_id.strip())


def _state_cell_id(state: Mapping[str, Any]) -> str | None:
    for blob in (
        state.get("send"),
        state.get("experiment"),
        state.get("work"),
        state.get("work_item"),
        state.get("cell"),
        state.get("evidence"),
        state.get("decision"),
        state,
    ):
        if isinstance(blob, Mapping) and blob.get("cell_id"):
            return str(blob["cell_id"])
    return None


WORK_CLAIM_OBSERVATION_FIELDS = (
    "claimant_agent_id",
    "lease_id",
    "lease_expires_at",
    "last_heartbeat_at",
)


def _empty_work_claim_observation() -> dict[str, None]:
    return {name: None for name in WORK_CLAIM_OBSERVATION_FIELDS}


def _work_claim_observation(lease: Mapping[str, Any] | None) -> dict[str, Any]:
    if lease is None:
        return _empty_work_claim_observation()
    return {
        "claimant_agent_id": lease.get("agent_id"),
        "lease_id": lease.get("lease_id"),
        "lease_expires_at": _jsonable(lease.get("expires_at")),
        "last_heartbeat_at": _jsonable(lease.get("updated_at")),
    }


def _work_item_view(state: Mapping[str, Any], lease: Mapping[str, Any] | None) -> dict[str, Any]:
    view = _jsonable(state)
    view.update(_work_claim_observation(lease))
    return view


def _named_pydantic_error(exc: PydanticValidationError) -> ValidationError:
    messages: list[str] = []
    for error in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in error.get("loc", ()))
        err_type = str(error.get("type") or "")
        msg = str(error.get("msg") or "is invalid")
        if not loc:
            messages.append(msg)
        elif err_type == "missing":
            messages.append(f"{loc} is required")
        elif err_type == "extra_forbidden":
            messages.append(f"{loc} is not permitted")
        else:
            messages.append(f"{loc}: {msg}")
    return ValidationError("; ".join(messages) if messages else "invalid payload")


STAGE_BUDGET_SCOPE = "stage"


def _lease_seconds(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LeaseError("lease_seconds must be an integer")
    if value <= 0 or value > 86_400:
        raise LeaseError("lease_seconds must be between 1 and 86400")
    return value


def _canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _threshold_holds(observed: Decimal, operator: str, target: Decimal) -> bool:
    comparisons = {
        "gt": observed > target,
        "gte": observed >= target,
        "eq": observed == target,
        "lte": observed <= target,
        "lt": observed < target,
    }
    try:
        return comparisons[operator]
    except KeyError as exc:
        raise ValidationError(f"unknown threshold operator {operator!r}") from exc


_PROHIBITED_EVENT_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "chain_of_thought",
        "credentials",
        "hidden_reasoning",
        "lease_token",
        "password",
        "private_key",
        "reasoning_trace",
        "secret",
        "token",
    }
)


def _validated_event_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _jsonable(payload)

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for raw_key, child in node.items():
                key = str(raw_key).casefold().replace("-", "_").replace(" ", "_")
                if key in _PROHIBITED_EVENT_KEYS or key.replace("_", "") in {
                    item.replace("_", "") for item in _PROHIBITED_EVENT_KEYS
                }:
                    raise ValidationError(f"prohibited key in domain event payload: {raw_key}")
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(normalized)
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if len(encoded) > 32_768:
        raise ValidationError("domain event payload exceeds 32768 bytes")
    return normalized


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        dumped = value.model_dump(mode="json")
        if isinstance(value, OfferCell):
            dumped["status"] = value.status.value
        return dumped
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(child) for child in value]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, StrEnum):
        return value.value
    return value


def _request_hash(tool_name: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"tool": tool_name, "payload": _jsonable(payload)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _operation_uuid(company_id: str, tool_name: str, key: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"ai-nature-company:{company_id}:{tool_name}:{key}")


def _principal_company(principal: VerifiedPrincipal) -> str:
    companies = {grant.scope.company_id for grant in principal.grants}
    if len(companies) != 1:
        raise AuthorizationError("principal must resolve to exactly one company scope")
    return next(iter(companies))


def _principal_uuid(principal: VerifiedPrincipal) -> UUID:
    return _uuid(principal.principal_id, "verified principal_id")


def _public_capability_name(value: str) -> str:
    variants = {
        Capability.ALLOCATION_COMMIT_BOUNDED.value: "allocation.commit",
        Capability.ALLOCATION_COMMIT_IRREVERSIBLE.value: "allocation.commit",
        Capability.CELL_TRANSITION_OPERATIONAL.value: "cell.transition",
        Capability.CELL_TRANSITION_PAUSE.value: "cell.transition",
        Capability.CELL_TRANSITION_IRREVERSIBLE.value: "cell.transition",
        Capability.CELL_TRANSITION_KILL_OWNER_SIGNED.value: "cell.transition",
        Capability.ECONOMICS_APPEND_ATTESTED.value: "economics.append",
        Capability.WORK_CREATE_ADMIN.value: "work.create",
    }
    return variants.get(value, value)


def _agent_instance_id(payload: Mapping[str, Any], definition_id: UUID) -> UUID:
    """Resolve the concrete agent instance while preserving the Phase 1 alias."""

    supplied = payload.get("agent_instance_id", payload.get("agent_id", definition_id))
    return _uuid(supplied, "agent_instance_id")


def _agent_instance_role(payload: Mapping[str, Any], definition: AgentDefinition) -> str:
    """Select one instance role without treating definition compatibility as authority."""

    compatible = {role.value for role in definition.compatible_roles}
    supplied = payload.get("role")
    if supplied is None:
        if len(compatible) != 1:
            raise ValidationError(
                "role is required when an agent definition has multiple compatible roles"
            )
        return next(iter(compatible))
    if not isinstance(supplied, str) or not supplied.strip():
        raise ValidationError("role must be a non-empty string")
    role = supplied.strip()
    if role not in compatible:
        raise ValidationError("agent instance role is not compatible with its definition")
    return role


class SubstrateService:
    """Authorized deterministic company operations over a transactional repository."""

    def __init__(
        self,
        repository: Repository,
        *,
        policy: AuthorizationPolicy | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.repository = repository
        self.policy = policy or AuthorizationPolicy()
        self.clock = clock or _now

    @property
    def store(self) -> Repository:
        """Stable alias used by replay and acceptance tooling."""

        return self.repository

    async def invoke(
        self,
        tool_name: str,
        auth_context: object,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if not is_verified_principal(auth_context):
            raise AuthorizationError("verified principal required")
        if not isinstance(payload, Mapping):
            raise ValidationError("payload must be an object")
        principal = auth_context
        try:
            if principal.agent_identity is not None:
                company_id = _principal_company(principal)
                async with self.repository.transaction(company_id) as tx:
                    await self._assert_agent_capabilities(tx, principal, {tool_name})
            if tool_name in READ_TOOLS:
                handler = getattr(self, f"_read_{tool_name.replace('.', '_')}", None)
                if handler is None:
                    raise NotFoundError(f"tool {tool_name!r} is not implemented")
                return await handler(principal, dict(payload))
            if tool_name not in MUTATION_TOOLS:
                raise NotFoundError(f"unknown substrate tool {tool_name!r}")
            handler = getattr(self, f"_mutate_{tool_name.replace('.', '_')}", None)
            if handler is None:
                raise NotFoundError(f"tool {tool_name!r} is not implemented")
            return await self._execute_mutation(tool_name, principal, dict(payload), handler)
        except PydanticValidationError as exc:
            raise ValidationError(str(exc)) from exc
        except SubstrateError:
            raise
        except Exception:
            LOGGER.exception("tool %s raised an unexpected exception", tool_name)
            raise InternalError("internal substrate error") from None

    async def tick(self, company_id: str) -> dict[str, Any]:
        """Apply frozen stop rules and lease expiry without model inference."""

        company_id = str(company_id).strip()
        if not company_id:
            raise ValidationError("company_id is required")
        principal = runtime_tick_principal(company_id)
        async with self.repository.transaction(company_id) as tx:
            if await tx.get_projection("company", company_id) is None:
                raise NotFoundError(f"company {company_id} does not exist")
            expired_work_item_ids = await self._tick_expire_leases(tx, principal, company_id)
            failed_overdue_work_item_ids = await self._tick_fail_overdue_open_work(
                tx, principal, company_id
            )
            closed_experiment_ids, closeout_work_item_ids = await self._tick_close_due_experiments(
                tx, principal, company_id
            )
        return {
            "inference_inside_substrate": False,
            "forces": ["maintain", "continue"],
            "closed_experiment_ids": closed_experiment_ids,
            "closeout_work_item_ids": closeout_work_item_ids,
            "expired_work_item_ids": expired_work_item_ids,
            "failed_overdue_work_item_ids": failed_overdue_work_item_ids,
        }

    async def maintenance_scan(
        self,
        company_id: str,
        *,
        agent_runs: Sequence[MaintenanceAgentRun] | MaintenanceAgentRunScan | None = None,
        scan_cap: int = DEFAULT_PROJECTION_SCAN_CAP,
        agent_run_cap: int = DEFAULT_AGENT_RUN_SCAN_CAP,
    ) -> dict[str, Any]:
        """Build one Maintain/Continue report on demand with zero writes.

        Nothing schedules this.  ``agent_runs`` overrides the transaction's
        run read (tests and offline replays); otherwise running runs are paged
        from the store inside the same read-only transaction.
        """

        company_id = str(company_id).strip()
        if not company_id:
            raise ValidationError("company_id is required")
        async with self.repository.transaction(company_id) as tx:
            readonly = ReadOnlyTransaction(tx)
            if await readonly.get_projection("company", company_id) is None:
                raise NotFoundError(f"company {company_id} does not exist")
            run_scan = (
                await scan_running_agent_runs(readonly, cap=agent_run_cap)
                if agent_runs is None
                else (
                    agent_runs
                    if isinstance(agent_runs, MaintenanceAgentRunScan)
                    else MaintenanceAgentRunScan(runs=tuple(agent_runs))
                )
            )
            return await self._build_maintenance_report(
                readonly,
                company_id=company_id,
                now=self.clock(),
                agent_runs=run_scan,
                scan_cap=scan_cap,
            )

    async def _build_maintenance_report(
        self,
        tx: ReadOnlyTransaction,
        *,
        company_id: str,
        now: datetime,
        agent_runs: MaintenanceAgentRunScan | None,
        scan_cap: int,
    ) -> dict[str, Any]:
        cell_scan = await scan_projections(tx, "cell", cap=scan_cap)
        work_scan = await scan_projections(tx, "work_item", cap=scan_cap)
        experiment_scan = await scan_projections(tx, "experiment", cap=scan_cap)
        evidence_scan = await scan_projections(tx, "evidence", cap=scan_cap)
        cells = [dict(record["state"]) for record in cell_scan.records]
        work_items = [dict(record["state"]) for record in work_scan.records]
        experiments = [dict(record["state"]) for record in experiment_scan.records]
        cell_status = {str(cell["cell_id"]): str(cell.get("status")) for cell in cells}
        evidence_types: dict[str, list[str]] = {}
        for record in evidence_scan.records:
            experiment_id = record["state"].get("experiment_id")
            evidence_type = record["state"].get("type")
            if experiment_id and evidence_type:
                evidence_types.setdefault(str(experiment_id), []).append(str(evidence_type))
        open_work_by_cell: dict[str, int] = {}
        for item in work_items:
            if item.get("status") in {"open", "claimed"} and item.get("cell_id"):
                key = str(item["cell_id"])
                open_work_by_cell[key] = open_work_by_cell.get(key, 0) + 1
        open_experiments_by_cell: dict[str, int] = {}
        views: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        closeout_work: dict[str, Mapping[str, Any] | None] = {}
        for state in experiments:
            experiment_id = str(state["experiment_id"])
            if state.get("state") in {"registered", "running", "evaluating"} and state.get(
                "cell_id"
            ):
                key = str(state["cell_id"])
                open_experiments_by_cell[key] = open_experiments_by_cell.get(key, 0) + 1
            view = await self._experiment_bet_view(
                tx,  # type: ignore[arg-type]
                state,
                evidence_types=tuple(evidence_types.get(experiment_id, ())),
            )
            views.append((state, view))
            closeout_id = str(uuid5(_uuid(experiment_id, "experiment_id"), "closeout-evaluation"))
            closeout = await tx.get_projection("work_item", closeout_id)
            closeout_work[experiment_id] = dict(closeout["state"]) if closeout else None

        findings: list[dict[str, Any]] = []
        if agent_runs is not None:
            findings.extend(agent_run_findings(agent_runs.runs, now=now, cell_status=cell_status))
        findings.extend(await work_findings(tx, work_items, now=now))
        findings.extend(experiment_findings(views, now=now, closeout_work=closeout_work))
        # Idleness can only be proven when work and experiments were fully scanned.
        idle_inputs_truncated = work_scan.truncated or experiment_scan.truncated
        if not idle_inputs_truncated:
            findings.extend(
                idle_cell_findings(
                    cells,
                    open_work_by_cell=open_work_by_cell,
                    open_experiments_by_cell=open_experiments_by_cell,
                )
            )
        total_findings = len(findings)
        findings_truncated = total_findings > MAX_REPORT_FINDINGS
        counts = summarize(findings)
        counts_by_cell: dict[str, dict[str, int]] = {}
        for item in findings:
            if item.get("cell_id"):
                bucket = counts_by_cell.setdefault(str(item["cell_id"]), {})
                bucket[item["kind"]] = bucket.get(item["kind"], 0) + 1
        findings = findings[:MAX_REPORT_FINDINGS]
        sections: dict[str, Any] = {
            "agent_runs": (
                section_status(
                    scanned=len(agent_runs.runs),
                    # Killed-cell checks need the cell map; a truncated cell
                    # scan means runs on unscanned cells may be missed.
                    truncated=agent_runs.truncated or cell_scan.truncated,
                    cap=agent_runs.cap,
                )
                if agent_runs is not None
                else {"status": "not_evaluated", "truncated": False, "scanned": None, "cap": None}
            ),
            "work_leases": section_status(
                scanned=len(work_items), truncated=work_scan.truncated, cap=scan_cap
            ),
            "experiments": section_status(
                scanned=len(experiments),
                truncated=experiment_scan.truncated or evidence_scan.truncated,
                cap=scan_cap,
            ),
            "idle_cells": section_status(
                scanned=len(cells),
                truncated=cell_scan.truncated or idle_inputs_truncated,
                cap=scan_cap,
            ),
            "findings": section_status(
                scanned=total_findings, truncated=findings_truncated, cap=MAX_REPORT_FINDINGS
            ),
            # TODO(learning 13): list founder-only steps per cell that no live
            # binding can sign (owner-signed path with founder_decision_ref or
            # a founder principal).  Needs a pending-founder-step model first.
            "founder_signing_paths": {
                "status": "todo",
                "truncated": False,
                "scanned": None,
                "cap": None,
            },
        }
        return {
            "report_version": MAINTENANCE_REPORT_VERSION,
            "company_id": company_id,
            "mode": "on_demand",
            "generated_at": _jsonable(now),
            "state_mutations": 0,
            "inference_inside_substrate": False,
            "forces": ["maintain", "continue"],
            "sections": sections,
            "truncated": any(item["truncated"] for item in sections.values()),
            "scanned": {
                "cells": len(cells),
                "work_items": len(work_items),
                "experiments": len(experiments),
                "evidence": len(evidence_scan.records),
                "agent_runs": len(agent_runs.runs) if agent_runs is not None else None,
            },
            "counts": counts,
            "counts_by_cell": dict(sorted(counts_by_cell.items())),
            "scanned_by_cell": _scanned_by_cell(
                cells=cells,
                work_items=work_items,
                experiments=experiments,
                evidence=evidence_scan.records,
                agent_runs=agent_runs.runs if agent_runs is not None else None,
            ),
            "total_findings": total_findings,
            "findings": findings,
            "findings_truncated": findings_truncated,
            "would_do": [
                {
                    "action": item["would_do"],
                    "kind": item["kind"],
                    "resource_type": item["resource_type"],
                    "resource_id": item["resource_id"],
                    "cell_id": item["cell_id"],
                }
                for item in findings
                if item["would_do"]
            ],
        }

    async def _execute_mutation(
        self,
        tool_name: str,
        principal: VerifiedPrincipal,
        envelope: dict[str, Any],
        handler: Callable[..., Any],
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        key = _required_str(envelope, "idempotency_key")
        expected_revision = envelope.get("expected_revision")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise ValidationError("expected_revision must be an integer")
        if expected_revision < 0:
            raise ValidationError("expected_revision must not be negative")
        correlation_id = _uuid(envelope.get("correlation_id"), "correlation_id")
        intent = _required_str(envelope, "operation_intent")
        if intent != tool_name:
            raise ValidationError("operation_intent must match the invoked tool")
        operation_payload = envelope.get("payload")
        if not isinstance(operation_payload, Mapping):
            raise ValidationError("mutation payload must be an object")
        digest = _request_hash(tool_name, envelope)
        operation_id = _operation_uuid(company_id, tool_name, key)

        async with self.repository.transaction(company_id) as tx:
            claim_time = self.clock()
            namespace = f"{tool_name}:{principal.principal_id}"
            existing = await tx.get_idempotency(namespace, key)
            claim = await tx.claim_idempotency(
                namespace=namespace,
                key=key,
                request_hash=digest,
                now=claim_time,
            )
            if claim["status"] == "completed":
                replay = dict(claim["response"])
                replay["replayed"] = True
                return replay
            if claim["status"] == "failed":
                raise ConflictError("the idempotent operation previously failed")
            existing_is_live = existing is not None and (
                existing.get("expires_at") is None or existing["expires_at"] > claim_time
            )
            if existing_is_live:
                raise ConflictError("the idempotent operation is already in progress")

            result = await handler(
                tx,
                principal,
                dict(operation_payload),
                expected_revision,
                operation_id,
                correlation_id,
                key,
            )
            if not isinstance(result, Mapping):
                raise TypeError("mutation handlers must return a mapping")
            state = await self._attach_work_claim_observation(tx, _jsonable(dict(result)))
            event_ids = list(state.pop("_event_ids", []))
            prior_revision = int(state.pop("_prior_revision", expected_revision))
            new_revision = int(state.pop("_new_revision", prior_revision + 1))
            audit_id = str(uuid5(operation_id, "audit"))
            details = {"operation_id": str(operation_id), "event_count": len(event_ids)}
            extra_details = state.pop("_audit_details", None)
            if isinstance(extra_details, Mapping):
                details.update(_jsonable(dict(extra_details)))
            if "cell_id" not in details:
                cell_id = _state_cell_id(state)
                if cell_id is not None:
                    details["cell_id"] = cell_id
            await tx.append_audit_event(
                audit_id=audit_id,
                action=tool_name,
                outcome="committed",
                actor_id=principal.principal_id,
                resource_type=str(state.pop("_resource_type", "operation")),
                resource_id=str(state.pop("_resource_id", operation_id)),
                correlation_id=str(correlation_id),
                details=details,
                occurred_at=self.clock(),
            )
            response = {
                "operation_id": str(operation_id),
                "prior_revision": prior_revision,
                "new_revision": new_revision,
                "event_ids": event_ids,
                "state": state,
                "audit_id": audit_id,
                "replayed": False,
            }
            await tx.complete_idempotency(
                namespace=namespace,
                key=key,
                request_hash=digest,
                response=response,
                now=self.clock(),
            )
            return response

    def _require(
        self,
        principal: VerifiedPrincipal,
        capability: Capability,
        resource: ResourceScope,
    ) -> None:
        self.policy.require(principal, capability, resource)

    async def _projection(
        self,
        tx: StoreTransaction,
        projection_type: str,
        projection_id: object,
        model: type[BaseModel] | None = None,
    ) -> tuple[dict[str, Any], BaseModel | None]:
        record = await tx.get_projection(projection_type, str(projection_id))
        if record is None:
            raise NotFoundError(f"{projection_type} {projection_id!s} does not exist")
        state = dict(record["state"])
        if model is None:
            parsed = None
        else:
            parsed = model.model_validate(
                {key: value for key, value in state.items() if key in model.model_fields}
            )
        return record, parsed

    async def _one_active_budget(
        self,
        tx: StoreTransaction,
        *,
        scope: str,
        stage: str | None = None,
    ) -> dict[str, Any]:
        records = await tx.list_budgets(
            scope=scope,
            stage=stage,
            status="active",
            limit=2,
        )
        label = f"{stage} stage" if stage is not None else scope
        if not records:
            raise NotFoundError(f"active {label} budget does not exist")
        if len(records) != 1:
            raise ConflictError(f"multiple active {label} budgets exist")
        return records[0]

    async def _constraint_ledgers(
        self,
        tx: StoreTransaction,
        *,
        cell_state: Mapping[str, Any],
        stage: str,
    ) -> dict[str, dict[str, Any]]:
        if stage not in {
            AllocationStage.EXPLORATION.value,
            AllocationStage.EXPLOITATION.value,
        }:
            raise ValidationError("funding requires exploration or exploitation stage")
        cell_budget = await tx.get_budget(str(cell_state["budget_id"]))
        if cell_budget is None:
            raise NotFoundError("cell budget does not exist")
        parent_budget_id = cell_budget.get("parent_budget_id")
        if parent_budget_id is None:
            company_budget = await self._one_active_budget(tx, scope=BudgetScope.COMPANY.value)
        else:
            company_budget = await tx.get_budget(str(parent_budget_id))
            if company_budget is None or company_budget.get("status") != "active":
                raise NotFoundError("active company budget does not exist")
        stage_budget = await self._one_active_budget(
            tx,
            scope=STAGE_BUDGET_SCOPE,
            stage=stage,
        )
        if company_budget.get("scope") != BudgetScope.COMPANY.value:
            raise ConflictError("cell budget parent is not the company ledger")
        if stage_budget.get("parent_budget_id") != company_budget["budget_id"]:
            raise ConflictError("stage budget is not linked to the company ledger")
        identities = {
            identity_of(company_budget),
            identity_of(cell_budget),
            identity_of(stage_budget),
        }
        if len(identities) != 1:
            raise BudgetError("company, cell, and stage ledgers must share a unit of account")
        return {
            "company": company_budget,
            "cell": cell_budget,
            "stage": stage_budget,
        }

    async def _list_constraint_ledgers(
        self,
        tx: StoreTransaction,
        *,
        status: str | None = "active",
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for scope in (BudgetScope.COMPANY.value, BudgetScope.CELL.value, STAGE_BUDGET_SCOPE):
            rows.extend(await tx.list_budgets(scope=scope, status=status, limit=1_000))
        return rows

    async def _close_constraint_ledger(
        self,
        tx: StoreTransaction,
        budget: Mapping[str, Any],
        *,
        superseded_by_budget_id: str,
        at: datetime,
    ) -> dict[str, Any]:
        metadata = dict(budget.get("metadata") or {})
        metadata["superseded_by_budget_id"] = superseded_by_budget_id
        metadata["closed_reason"] = "constitution.amend"
        return await tx.update_budget(
            budget_id=str(budget["budget_id"]),
            expected_revision=int(budget["revision"]),
            status="closed",
            metadata=metadata,
            now=at,
        )

    async def _realign_constraint_ledgers(
        self,
        tx: StoreTransaction,
        *,
        company_id: str,
        operation_id: UUID,
        budget_policy: BudgetPolicy,
        at: datetime,
    ) -> dict[str, Any]:
        """Point company, stage, and cell constraint ledgers at one unit of account."""

        target_identity = (
            budget_policy.unit.value,
            budget_policy.currency if budget_policy.unit.value == MONEY else None,
        )
        target_unit, target_currency = target_identity
        active = await self._list_constraint_ledgers(tx, status="active")
        if not active:
            raise NotFoundError("active constraint ledgers do not exist")
        identities = {identity_of(row) for row in active}
        company_ledgers = [row for row in active if row["scope"] == BudgetScope.COMPANY.value]
        if len(company_ledgers) != 1:
            raise ConflictError("company must have exactly one active constraint ledger")
        company_budget = company_ledgers[0]
        exploration_limit_minor = (
            budget_policy.company_limit_minor * budget_policy.exploration_share_bps // 10_000
        )
        exploitation_limit_minor = budget_policy.company_limit_minor - exploration_limit_minor
        stage_limits = {
            AllocationStage.EXPLORATION.value: exploration_limit_minor,
            AllocationStage.EXPLOITATION.value: exploitation_limit_minor,
        }

        if identities == {target_identity}:
            updated: list[dict[str, Any]] = []
            for row in active:
                current_limit = ledger_to_quantity(row["limit"], row["unit"], "budget.limit")
                if row["scope"] == BudgetScope.COMPANY.value:
                    next_limit = budget_policy.company_limit_minor
                elif row["scope"] == STAGE_BUDGET_SCOPE:
                    next_limit = stage_limits[str(row["stage"])]
                else:
                    next_limit = min(current_limit, budget_policy.cell_limit_minor)
                if next_limit == current_limit:
                    updated.append(dict(row))
                    continue
                updated.append(
                    await tx.update_budget(
                        budget_id=str(row["budget_id"]),
                        expected_revision=int(row["revision"]),
                        limit=quantity_to_ledger(next_limit, target_unit),
                        now=at,
                    )
                )
            return {
                "unit": target_unit,
                "currency": target_currency,
                "realigned": False,
                "closed": [],
                "active": updated,
            }

        replacement_ids: dict[str, str] = {
            str(company_budget["budget_id"]): str(uuid5(operation_id, "company-budget"))
        }
        for row in active:
            budget_id = str(row["budget_id"])
            if budget_id in replacement_ids:
                continue
            if row["scope"] == STAGE_BUDGET_SCOPE:
                replacement_ids[budget_id] = str(uuid5(operation_id, f"stage:{row['stage']}"))
            else:
                replacement_ids[budget_id] = str(
                    uuid5(operation_id, f"cell-budget:{row.get('cell_id') or budget_id}")
                )

        closed = [
            await self._close_constraint_ledger(
                tx,
                row,
                superseded_by_budget_id=replacement_ids[str(row["budget_id"])],
                at=at,
            )
            for row in active
        ]

        company_ends = at + timedelta(days=365)
        new_company_id = replacement_ids[str(company_budget["budget_id"])]
        stage_budget_ids = {
            AllocationStage.EXPLORATION.value: str(uuid5(operation_id, "stage:exploration")),
            AllocationStage.EXPLOITATION.value: str(uuid5(operation_id, "stage:exploitation")),
        }
        created: list[dict[str, Any]] = [
            await tx.create_budget(
                budget_id=new_company_id,
                scope=BudgetScope.COMPANY.value,
                limit=quantity_to_ledger(budget_policy.company_limit_minor, target_unit),
                unit=target_unit,
                currency=target_currency,
                starts_at=at,
                ends_at=company_ends,
                metadata={
                    "ledger_kind": "constraint",
                    "replaces_budget_id": str(company_budget["budget_id"]),
                    "stage_budget_ids": stage_budget_ids,
                },
                now=at,
            )
        ]
        for stage, stage_limit_minor in stage_limits.items():
            prior_stage = next(
                (row for row in active if row.get("stage") == stage),
                None,
            )
            created.append(
                await tx.create_budget(
                    budget_id=stage_budget_ids[stage],
                    scope=STAGE_BUDGET_SCOPE,
                    parent_budget_id=new_company_id,
                    stage=stage,
                    limit=quantity_to_ledger(stage_limit_minor, target_unit),
                    unit=target_unit,
                    currency=target_currency,
                    starts_at=at,
                    ends_at=company_ends,
                    metadata={
                        "ledger_kind": "constraint",
                        "stage": stage,
                        **(
                            {"replaces_budget_id": str(prior_stage["budget_id"])}
                            if prior_stage is not None
                            else {}
                        ),
                    },
                    now=at,
                )
            )

        cell_records = await tx.list_projections(projection_type="cell", limit=500)
        for cell_record in cell_records:
            cell_state = dict(cell_record["state"])
            prior_budget_id = str(cell_state["budget_id"])
            new_cell_budget_id = replacement_ids.get(
                prior_budget_id,
                str(uuid5(operation_id, f"cell-budget:{cell_state['cell_id']}")),
            )
            created.append(
                await tx.create_budget(
                    budget_id=new_cell_budget_id,
                    scope=BudgetScope.CELL.value,
                    parent_budget_id=new_company_id,
                    cell_id=str(cell_state["cell_id"]),
                    limit=quantity_to_ledger(budget_policy.cell_limit_minor, target_unit),
                    unit=target_unit,
                    currency=target_currency,
                    starts_at=at,
                    ends_at=at + timedelta(days=90),
                    metadata={
                        "ledger_kind": "constraint",
                        "replaces_budget_id": prior_budget_id,
                    },
                    now=at,
                )
            )
            cell_state["budget_id"] = new_cell_budget_id
            if "revision" in cell_state:
                cell_state["revision"] = int(cell_state["revision"]) + 1
            cell_state["updated_at"] = _jsonable(at)
            await self._put_state(
                tx,
                "cell",
                cell_state["cell_id"],
                cell_state,
                expected_projection_revision=cell_record["revision"],
            )

        return {
            "unit": target_unit,
            "currency": target_currency,
            "realigned": True,
            "closed": closed,
            "active": created,
        }

    async def _put_model(
        self,
        tx: StoreTransaction,
        projection_type: str,
        projection_id: object,
        model: BaseModel,
        *,
        expected_projection_revision: int,
    ) -> dict[str, Any]:
        state = _jsonable(model)
        if isinstance(model, OfferCell):
            state["status"] = model.status.value
        return await tx.put_projection(
            projection_type=projection_type,
            projection_id=str(projection_id),
            state=state,
            expected_revision=expected_projection_revision,
            updated_at=self.clock(),
        )

    async def _append_event(
        self,
        tx: StoreTransaction,
        *,
        operation_id: UUID,
        suffix: str,
        aggregate_type: str,
        aggregate_id: object,
        event_type: str,
        principal: VerifiedPrincipal,
        payload: Mapping[str, Any],
        correlation_id: UUID,
        idempotency_key: str,
    ) -> str:
        event_id = str(uuid5(operation_id, suffix))
        metadata: dict[str, Any] = {}
        if principal.agent_run_id is not None:
            metadata["agent_run_id"] = principal.agent_run_id
        if principal.agent_identity is not None:
            metadata.update(
                {
                    "agent_instance_id": principal.agent_identity.agent_instance_id,
                    "agent_definition_id": principal.agent_identity.definition_id,
                    "agent_definition_version": principal.agent_identity.definition_version,
                    "agent_role": principal.agent_identity.role.value,
                    "agent_cell_id": principal.agent_identity.cell_id,
                }
            )
        binding = await tx.get_projection("agent_binding", principal.principal_id)
        if binding is not None:
            binding_metadata = {
                "agent_instance_id": binding["state"].get("agent_instance_id")
                or binding["state"].get("agent_id"),
                "agent_definition_id": binding["state"].get("definition_id"),
                "agent_definition_version": binding["state"].get("definition_version"),
                "agent_role": binding["state"].get("role"),
                "agent_cell_id": binding["state"].get("cell_id"),
            }
            for key, value in binding_metadata.items():
                if key not in metadata:
                    metadata[key] = value
        await tx.append_event(
            event_id=event_id,
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            event_type=event_type,
            actor_id=principal.principal_id,
            payload=_validated_event_payload(payload),
            correlation_id=str(correlation_id),
            idempotency_key=idempotency_key,
            occurred_at=self.clock(),
            metadata={key: value for key, value in metadata.items() if value is not None},
        )
        return event_id

    async def _binding(self, tx: StoreTransaction, principal: VerifiedPrincipal) -> dict[str, Any]:
        record, _ = await self._projection(tx, "agent_binding", principal.principal_id)
        binding = dict(record["state"])
        if binding.get("status", "active") != "active":
            raise AuthorizationError("agent binding is not active")
        agent_instance_id = (
            binding.get("agent_instance_id")
            or binding.get("agent_id")
            or binding.get("definition_id")
        )
        agent_record, _ = await self._projection(tx, "agent", agent_instance_id)
        instance = dict(agent_record["state"])
        if instance.get("status", "active") != "active":
            raise AuthorizationError("agent instance is not active")
        if instance.get("principal_id") != principal.principal_id:
            raise AuthorizationError("agent binding principal does not match its instance")

        identity = principal.agent_identity
        if identity is not None:
            expected = {
                "agent_instance_id": identity.agent_instance_id,
                "definition_id": identity.definition_id,
                "definition_version": identity.definition_version,
                "role": identity.role.value,
                "cell_id": identity.cell_id,
            }
            actual = {
                "agent_instance_id": str(
                    instance.get("agent_instance_id") or instance.get("agent_id")
                ),
                "definition_id": str(instance.get("definition_id")),
                "definition_version": instance.get("definition_version"),
                "role": instance.get("role"),
                "cell_id": instance.get("cell_id"),
            }
            if actual != expected:
                raise AuthorizationError("verified agent identity does not match its binding")

        definition_id = instance.get("definition_id")
        definition_version = instance.get("definition_version")
        definition_record = await tx.get_projection("agent_definition", str(definition_id))
        if definition_record is None and identity is not None:
            raise AuthorizationError("registered agent definition does not exist")
        if definition_record is not None:
            try:
                definition = AgentDefinition.model_validate(definition_record["state"])
            except PydanticValidationError as exc:
                raise AuthorizationError("registered agent definition is invalid") from exc
            if (
                str(definition.definition_id) != str(definition_id)
                or definition.version != definition_version
            ):
                raise AuthorizationError("agent instance does not match its definition version")
            instance["capabilities"] = sorted(definition.declared_capabilities)
            instance["definition"] = definition.model_dump(mode="json")
        return _expand_live_operating_tools(instance)

    async def _put_state(
        self,
        tx: StoreTransaction,
        projection_type: str,
        projection_id: object,
        state: Mapping[str, Any],
        *,
        expected_projection_revision: int,
    ) -> dict[str, Any]:
        return await tx.put_projection(
            projection_type=projection_type,
            projection_id=str(projection_id),
            state=_jsonable(dict(state)),
            expected_revision=expected_projection_revision,
            updated_at=self.clock(),
        )

    async def _ensure_agent_definition(
        self,
        tx: StoreTransaction,
        definition: AgentDefinition,
    ) -> dict[str, Any]:
        """Persist one immutable reusable definition or verify the existing value."""

        definition_id = str(definition.definition_id)
        canonical = definition.model_dump(mode="json")
        existing = await tx.get_projection("agent_definition", definition_id)
        if existing is None:
            await self._put_state(
                tx,
                "agent_definition",
                definition_id,
                canonical,
                expected_projection_revision=0,
            )
            return canonical
        try:
            registered = AgentDefinition.model_validate(existing["state"])
        except PydanticValidationError as exc:
            raise ConflictError("registered agent definition is invalid") from exc
        if registered != definition:
            raise ConflictError(
                "definition_id already identifies another immutable agent definition"
            )
        return canonical

    @staticmethod
    def _revised_state(
        record_state: Mapping[str, Any], expected_revision: int, **changes: Any
    ) -> dict[str, Any]:
        current = record_state.get("revision")
        if current != expected_revision:
            raise ConflictError(
                f"stale aggregate revision: expected {expected_revision}, current {current}"
            )
        revised = dict(record_state)
        revised.update(changes)
        revised["revision"] = expected_revision + 1
        return revised

    @staticmethod
    def _principal_run_uuid(principal: VerifiedPrincipal) -> UUID:
        if principal.agent_run_id is None:
            raise AuthorizationError("an active server-derived agent run is required")
        return _uuid(principal.agent_run_id, "verified agent_run_id")

    async def _assert_agent_capabilities(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        required: set[str] | frozenset[str],
        *,
        cell_id: str | None = None,
        require_bound_cell_target: bool = False,
    ) -> dict[str, Any]:
        try:
            binding = await self._binding(tx, principal)
        except NotFoundError:
            # A human founder may act without an agent manifest, but only when a
            # founder grant applies at the current company root.  The union of a
            # principal's roles is not authority: a narrow founder grant must not
            # combine with a broader non-founder grant to bypass agent binding.
            try:
                self.policy.require_founder_action(
                    principal,
                    ResourceScope.company(_principal_company(principal)),
                    action="agent_binding.bypass",
                )
            except AuthorizationError:
                raise AuthorizationError("active agent binding is required") from None
            return {}
        bound_cell = binding.get("cell_id")
        if bound_cell is not None and cell_id is None and require_bound_cell_target:
            raise AuthorizationError("agent binding does not cover a company-scoped target")
        if bound_cell is not None and cell_id is not None and str(bound_cell) != str(cell_id):
            raise AuthorizationError("agent binding does not cover this cell")
        declared = set(binding.get("capabilities", ()))
        allowed = set(binding.get("allowed_actions", declared))
        public_required = {_public_capability_name(value) for value in required}
        if not public_required <= (declared & allowed):
            raise AuthorizationError("agent definition does not permit this operation")
        return binding

    async def _assert_offer_price_floor(
        self,
        tx: StoreTransaction,
        company_id: str,
        offer: Offer,
    ) -> None:
        company_record, _ = await self._projection(tx, "company", company_id)
        constitution_record, _ = await self._projection(
            tx,
            "constitution",
            company_record["state"]["active_constitution_id"],
        )
        floor = int(constitution_record["state"]["risk_policy"]["pricing_floor_minor"])
        if offer.price.amount_minor < floor:
            raise ValidationError("offer price is below the constitution pricing floor")

    def _assert_lease_owner(self, lease: Mapping[str, Any], principal: VerifiedPrincipal) -> None:
        if lease.get("agent_id") != principal.principal_id:
            raise LeaseError("lease belongs to another principal")
        if lease.get("agent_run_id") != principal.agent_run_id:
            raise LeaseError("lease belongs to another agent run")

    @staticmethod
    def _check_revision(model: BaseModel, expected_revision: int) -> None:
        current = getattr(model, "revision", None)
        if current != expected_revision:
            raise ConflictError(
                f"stale aggregate revision: expected {expected_revision}, current {current}"
            )

    async def _list_active_ledger_rows(self, tx: StoreTransaction) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for budget in await tx.list_budgets(limit=1_000):
            if budget.get("status") != "active":
                continue
            snap = _budget_snapshot(budget)
            row = {
                "budget_id": snap["budget_id"],
                "scope": snap["scope"],
                "unit": snap["unit"],
                "available_minor": snap["available_minor"],
                "limit_minor": snap["limit_minor"],
                "spent_minor": snap["spent_minor"],
                "reserved_minor": snap["reserved_minor"],
            }
            if snap.get("stage") is not None:
                row["stage"] = snap["stage"]
            if snap.get("currency") is not None:
                row["currency"] = snap["currency"]
            if budget.get("cell_id"):
                row["cell_id"] = str(budget["cell_id"])
            rows.append(row)
        rows.sort(key=lambda item: (item["scope"], str(item.get("stage") or ""), item["budget_id"]))
        return rows

    async def _evidence_types_by_experiment(
        self, tx: StoreTransaction
    ) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, list[str]] = {}
        for record in await tx.list_projections(projection_type="evidence", limit=1_000):
            experiment_id = record["state"].get("experiment_id")
            evidence_type = record["state"].get("type")
            if experiment_id and evidence_type:
                grouped.setdefault(str(experiment_id), []).append(str(evidence_type))
        return {key: tuple(values) for key, values in grouped.items()}

    async def _qualifying_fitness_evidence_ids(
        self, tx: StoreTransaction, experiment_id: str
    ) -> tuple[str, ...]:
        """Return fitness-class evidence IDs already on the experiment ledger."""

        found: list[str] = []
        for record in await tx.list_projections(projection_type="evidence", limit=1_000):
            state = record["state"]
            if str(state.get("experiment_id") or "") != experiment_id:
                continue
            if classify_evidence(state) != "fitness":
                continue
            evidence_id = state.get("evidence_id")
            if evidence_id:
                found.append(str(evidence_id))
        found.sort()
        return tuple(found)

    async def _cell_operator_counts(self, tx: StoreTransaction, cell_id: str) -> dict[str, int]:
        now = self.clock()
        activity = 0
        fitness = 0
        stale = 0
        for record in await tx.list_projections(projection_type="evidence", limit=1_000):
            state = record["state"]
            if str(state.get("cell_id") or "") != cell_id:
                continue
            kind = classify_evidence(state)
            if kind == "activity":
                activity += 1
            elif kind == "fitness":
                fitness += 1
        for record in await tx.list_projections(projection_type="work_item", limit=1_000):
            if (
                _is_stale_open_work(record["state"], now)
                and str(record["state"].get("cell_id") or "") == cell_id
            ):
                stale += 1
        return {
            "activity_evidence_count": activity,
            "fitness_evidence_count": fitness,
            "stale_open_work_count": stale,
        }

    async def _work_item_cell_index(self, tx: StoreTransaction) -> dict[str, str]:
        index: dict[str, str] = {}
        for record in await tx.list_projections(projection_type="work_item", limit=1_000):
            cell_id = record["state"].get("cell_id")
            if not cell_id:
                continue
            cell = str(cell_id)
            index[str(record["projection_id"])] = cell
            work_id = record["state"].get("work_item_id")
            if work_id:
                index[str(work_id)] = cell
        return index

    async def _observed_work_item(
        self, tx: StoreTransaction, state: Mapping[str, Any]
    ) -> dict[str, Any]:
        work_id = state.get("work_item_id")
        lease = None
        if work_id:
            lease = await tx.get_lease(str(work_id), include_inactive=True, now=self.clock())
        return _work_item_view(state, lease)

    async def _attach_work_claim_observation(
        self, tx: StoreTransaction, state: dict[str, Any]
    ) -> dict[str, Any]:
        raw = state.get("work_item")
        if not isinstance(raw, Mapping):
            return state
        state["work_item"] = await self._observed_work_item(tx, raw)
        return state

    async def _cell_work_summaries(
        self, tx: StoreTransaction, cell_id: str
    ) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for record in await tx.list_projections(
            projection_type="work_item", cell_id=cell_id, limit=500
        ):
            view = await self._observed_work_item(tx, record["state"])
            summaries.append(
                {
                    "work_item_id": view.get("work_item_id"),
                    "status": view.get("status"),
                    "kind": view.get("kind"),
                    "revision": view.get("revision"),
                    "claimant_agent_id": view.get("claimant_agent_id"),
                    "lease_id": view.get("lease_id"),
                    "lease_expires_at": view.get("lease_expires_at"),
                    "last_heartbeat_at": view.get("last_heartbeat_at"),
                }
            )
        return summaries

    async def _experiment_bet_view(
        self,
        tx: StoreTransaction,
        experiment: Mapping[str, Any],
        *,
        evidence_types: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        experiment_id = str(experiment["experiment_id"])
        actual_minor = 0
        actual_cost = experiment.get("actual_cost")
        if isinstance(actual_cost, Mapping) and actual_cost.get("amount_minor") is not None:
            actual_minor = int(actual_cost["amount_minor"])
        runtime = await tx.get_projection("experiment_runtime", experiment_id)
        if runtime is not None and runtime["state"].get("actual_cost_minor") is not None:
            actual_minor = int(runtime["state"]["actual_cost_minor"])
        assessment = experiment_stop_assessment(
            experiment,
            now=self.clock(),
            actual_cost_minor=actual_minor,
            evidence_types=evidence_types,
        )
        status = str(experiment.get("state") or experiment.get("status") or "")
        view = {
            "experiment_id": experiment_id,
            "state": status,
            "experiment_state": status,
            "stop_state": assessment["stop_state"],
            "fired_rules": assessment["fired_rules"],
            "close_eligible": assessment["close_eligible"],
            "window_ends_at": assessment["window_ends_at"],
            "remaining_budget_minor": assessment["remaining_budget_minor"],
            "unit": assessment["unit"],
        }
        if assessment.get("currency") is not None:
            view["currency"] = assessment["currency"]
        if experiment.get("reason"):
            view["reason"] = experiment["reason"]
        if experiment.get("founder_decision_ref"):
            view["founder_decision_ref"] = experiment["founder_decision_ref"]
        runtime_state = runtime["state"] if runtime is not None else {}
        recorded_send_count = len(runtime_state.get("recorded_send_ids") or ())
        view["recorded_send_count"] = recorded_send_count
        view["spent_minor"] = actual_minor
        override = None
        try:
            override = _experiment_message_override(experiment)
        except ValidationError:
            return view
        if override is not None:
            cap = override
            view["max_external_messages"] = override
        else:
            try:
                company_record, _ = await self._projection(
                    tx, "company", str(experiment["company_id"])
                )
                constitution_record, _ = await self._projection(
                    tx,
                    "constitution",
                    company_record["state"]["active_constitution_id"],
                )
                cap = _external_message_cap(constitution_record["state"])
            except (KeyError, TypeError, ValidationError, NotFoundError):
                return view
        view["max_external_messages_per_experiment"] = cap
        view["external_messages_remaining"] = _external_messages_remaining(
            cap=cap,
            recorded_send_count=recorded_send_count,
        )
        return view

    @staticmethod
    def _cell_bet_view(
        cell: Mapping[str, Any],
        *,
        experiments: list[Mapping[str, Any]],
        ledger_rows: list[Mapping[str, Any]],
        activity_evidence_count: int = 0,
        fitness_evidence_count: int = 0,
        stale_open_work_count: int = 0,
    ) -> dict[str, Any]:
        status = str(cell.get("status") or cell.get("state") or "")
        cell_ledgers = [row for row in ledger_rows if row.get("scope") == "cell"]
        remaining = int(cell_ledgers[0]["available_minor"]) if cell_ledgers else 0
        spent = int(cell_ledgers[0]["spent_minor"]) if cell_ledgers else 0
        return {
            "cell_id": str(cell["cell_id"]),
            "status": status,
            "objective": cell.get("objective"),
            "amendment_history": list(cell.get("amendment_history") or ()),
            "remaining_budget_minor": remaining,
            "spent_minor": spent,
            "activity_evidence_count": activity_evidence_count,
            "fitness_evidence_count": fitness_evidence_count,
            "stale_open_work_count": stale_open_work_count,
            "eligibility": cell_operating_eligibility(
                cell_status=status,
                experiment_assessments=experiments,
            ),
            "experiments": list(experiments),
        }

    async def _apply_work_ledger_spend(
        self,
        tx: StoreTransaction,
        *,
        work_state: Mapping[str, Any],
        runtime: dict[str, Any],
        spend_minor: int,
        allow_partial: bool = False,
    ) -> int:
        """Debit an experiment reservation when Act work consumes its budget.

        Sense work observes into the shared medium and MUST NOT increment
        spent_minor. Act complete/fail spends through the existing reservation
        group so company, cell, and stage ledgers stay aligned. Zero-cost work
        and replayed completions are no-ops.
        """

        if work_kind_force(work_state.get("kind")) == "sense":
            runtime["ledger_spend_minor"] = 0
            runtime["force"] = "sense"
            return 0
        runtime.setdefault("force", "act")
        if runtime.get("ledger_spend_minor"):
            return 0
        if isinstance(spend_minor, bool) or not isinstance(spend_minor, int) or spend_minor < 0:
            raise ValidationError("work ledger spend must be a non-negative integer")
        if spend_minor == 0:
            return 0
        experiment_id = work_state.get("experiment_id")
        if experiment_id is None:
            return 0
        experiment_id = str(experiment_id)
        runtime_record = await tx.get_projection("experiment_runtime", experiment_id)
        if runtime_record is None:
            return 0
        experiment_runtime = dict(runtime_record["state"])
        reservation_group_id = str(
            experiment_runtime.get(
                "reservation_group_id", experiment_runtime.get("reservation_id") or ""
            )
        )
        if not reservation_group_id:
            return 0
        reservation = await tx.get_reservation_group(reservation_group_id)
        if reservation is None or reservation.get("status") != "active":
            return 0
        experiment_record = await tx.get_projection("experiment", experiment_id)
        if experiment_record is None:
            return 0
        experiment_unit, _currency = identity_of(experiment_record["state"]["budget"])
        already_minor = int(experiment_runtime.get("actual_cost_minor") or 0)
        hold_minor = ledger_to_quantity(
            reservation["amount"], experiment_unit, "reservation.amount"
        )
        applied_minor = ledger_to_quantity(
            reservation["actual_amount"] if reservation["actual_amount"] is not None else 0,
            experiment_unit,
            "reservation.actual_amount",
        )
        remaining_minor = hold_minor - applied_minor
        if remaining_minor <= 0:
            return 0
        debit = min(spend_minor, remaining_minor) if allow_partial else spend_minor
        if debit > remaining_minor:
            raise BudgetError("work cost exceeds remaining experiment reservation")
        if debit == 0:
            return 0
        await tx.apply_reservation_group_spend(
            reservation_group_id=reservation_group_id,
            amount=quantity_to_ledger(debit, experiment_unit),
            expected_revisions={
                member["reservation_id"]: member["revision"] for member in reservation["members"]
            },
            now=self.clock(),
        )
        experiment_runtime["actual_cost_minor"] = already_minor + debit
        await self._put_state(
            tx,
            "experiment_runtime",
            experiment_id,
            experiment_runtime,
            expected_projection_revision=runtime_record["revision"],
        )
        runtime["ledger_spend_minor"] = debit
        return debit

    async def _apply_experiment_try_spend(
        self,
        tx: StoreTransaction,
        *,
        experiment_id: str,
        spend_minor: int,
        extra_runtime: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Fail-closed Act debit against a running experiment reservation.

        Unlike work complete/fail, a missing or inactive reservation is an
        error. Used for owner-signed recorded external sends: exactly one try
        each, never a silent no-op.
        """

        if isinstance(spend_minor, bool) or not isinstance(spend_minor, int) or spend_minor <= 0:
            raise ValidationError("experiment try spend must be a positive integer")
        runtime_record, _ = await self._projection(tx, "experiment_runtime", experiment_id)
        experiment_runtime = dict(runtime_record["state"])
        reservation_group_id = str(
            experiment_runtime.get(
                "reservation_group_id", experiment_runtime.get("reservation_id") or ""
            )
        )
        if not reservation_group_id:
            raise BudgetError("experiment reservation is missing")
        reservation = await tx.get_reservation_group(reservation_group_id)
        if reservation is None or reservation.get("status") != "active":
            raise BudgetError("experiment reservation is not active")
        experiment_record, _ = await self._projection(tx, "experiment", experiment_id)
        experiment_unit, _currency = identity_of(experiment_record["state"]["budget"])
        if experiment_unit != "tries":
            raise ValidationError("experiment.record_send requires a tries budget")
        already_minor = int(experiment_runtime.get("actual_cost_minor") or 0)
        hold_minor = ledger_to_quantity(
            reservation["amount"], experiment_unit, "reservation.amount"
        )
        applied_minor = ledger_to_quantity(
            reservation["actual_amount"] if reservation["actual_amount"] is not None else 0,
            experiment_unit,
            "reservation.actual_amount",
        )
        remaining_minor = hold_minor - applied_minor
        if spend_minor > remaining_minor:
            raise BudgetError("recorded send exceeds remaining experiment reservation")
        await tx.apply_reservation_group_spend(
            reservation_group_id=reservation_group_id,
            amount=quantity_to_ledger(spend_minor, experiment_unit),
            expected_revisions={
                member["reservation_id"]: member["revision"] for member in reservation["members"]
            },
            now=self.clock(),
        )
        experiment_runtime["actual_cost_minor"] = already_minor + spend_minor
        if extra_runtime:
            experiment_runtime.update(dict(extra_runtime))
        persisted = await self._put_state(
            tx,
            "experiment_runtime",
            experiment_id,
            experiment_runtime,
            expected_projection_revision=runtime_record["revision"],
        )
        return persisted

    async def _adjust_experiment_reservation_hold(
        self,
        tx: StoreTransaction,
        *,
        experiment_id: str,
        experiment: Mapping[str, Any],
        new_amount_minor: int,
    ) -> Mapping[str, Any]:
        """Resize the experiment reservation through the same ledger path as start."""

        runtime_record, _ = await self._projection(tx, "experiment_runtime", experiment_id)
        experiment_runtime = dict(runtime_record["state"])
        reservation_group_id = str(
            experiment_runtime.get(
                "reservation_group_id", experiment_runtime.get("reservation_id") or ""
            )
        )
        if not reservation_group_id:
            raise BudgetError("experiment reservation is missing")
        reservation = await tx.get_reservation_group(reservation_group_id)
        if reservation is None or reservation.get("status") != "active":
            raise BudgetError("experiment reservation is not active")
        experiment_unit, _currency = identity_of(experiment["budget"])
        applied_minor = ledger_to_quantity(
            reservation["actual_amount"] if reservation["actual_amount"] is not None else 0,
            experiment_unit,
            "reservation.actual_amount",
        )
        already_minor = int(experiment_runtime.get("actual_cost_minor") or 0)
        spent_floor = max(already_minor, applied_minor)
        if new_amount_minor < spent_floor:
            raise BudgetError(
                f"budget_amount_minor cannot drop below already spent ({spent_floor})"
            )
        metadata = dict(reservation.get("metadata") or {})
        metadata["amount_minor"] = new_amount_minor
        return await tx.adjust_reservation_group_hold(
            reservation_group_id=reservation_group_id,
            amount=quantity_to_ledger(new_amount_minor, experiment_unit),
            expected_revisions={
                member["reservation_id"]: member["revision"] for member in reservation["members"]
            },
            metadata=metadata,
            now=self.clock(),
        )

    async def _tick_fail_overdue_open_work(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        company_id: str,
    ) -> list[str]:
        now = self.clock()
        failed_ids: list[str] = []
        for record in await tx.list_projections(projection_type="work_item", limit=1_000):
            state = dict(record["state"])
            if not _is_stale_open_work(state, now):
                continue
            work_id = str(state["work_item_id"])
            expected_revision = int(state["revision"])
            updated = self._revised_state(state, expected_revision, status=WorkStatus.FAILED.value)
            runtime_record, _ = await self._projection(tx, "work_runtime", work_id)
            runtime = dict(runtime_record["state"])
            runtime["lease_id"] = None
            runtime["failure_code"] = "deadline_elapsed"
            budget = state.get("budget") if isinstance(state.get("budget"), Mapping) else {}
            spend_minor = _minor_amount(
                budget.get("maximum_cost_minor", 0), "work.budget.maximum_cost_minor"
            )
            await self._apply_work_ledger_spend(
                tx,
                work_state=state,
                runtime=runtime,
                spend_minor=spend_minor,
                allow_partial=True,
            )
            await self._put_state(
                tx,
                "work_item",
                work_id,
                updated,
                expected_projection_revision=record["revision"],
            )
            await self._put_state(
                tx,
                "work_runtime",
                work_id,
                runtime,
                expected_projection_revision=runtime_record["revision"],
            )
            operation_id = uuid5(
                NAMESPACE_URL,
                f"ai-nature-company:{company_id}:runtime.tick:deadline:{work_id}",
            )
            await self._append_event(
                tx,
                operation_id=operation_id,
                suffix="work-deadline-elapsed",
                aggregate_type="work_item",
                aggregate_id=work_id,
                event_type="work.failed",
                principal=principal,
                payload={
                    "state": updated,
                    "failure_code": "deadline_elapsed",
                    "ledger_spend_minor": runtime.get("ledger_spend_minor", 0),
                },
                correlation_id=uuid5(NAMESPACE_URL, f"ai-nature-company:{company_id}:runtime.tick"),
                idempotency_key=f"runtime.tick:deadline:{work_id}",
            )
            failed_ids.append(work_id)
        failed_ids.sort()
        return failed_ids

    async def _tick_expire_leases(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        company_id: str,
    ) -> list[str]:
        now = self.clock()
        expired_ids: list[str] = []
        for record in await tx.list_projections(projection_type="work_item", limit=1_000):
            state = dict(record["state"])
            if state.get("status") != WorkStatus.CLAIMED.value:
                continue
            work_id = str(state["work_item_id"])
            lease = await tx.get_lease(work_id, include_inactive=True, now=now)
            if lease is None or lease.get("status") != "active" or lease.get("expires_at") > now:
                continue
            expired_lease = await tx.expire_lease(
                work_item_id=work_id,
                expected_revision=lease["revision"],
                now=now,
            )
            runtime_record, _ = await self._projection(tx, "work_runtime", work_id)
            runtime = dict(runtime_record["state"])
            attempt = int(runtime.get("attempt", 0))
            expected_revision = int(state["revision"])
            if attempt >= int(state["max_attempts"]):
                updated = self._revised_state(
                    state,
                    expected_revision,
                    status=WorkStatus.FAILED.value,
                )
                runtime["failure_code"] = "lease_expired"
            else:
                updated = self._revised_state(
                    state,
                    expected_revision,
                    status=WorkStatus.OPEN.value,
                )
            runtime["lease_id"] = None
            await self._put_state(
                tx,
                "work_item",
                work_id,
                updated,
                expected_projection_revision=record["revision"],
            )
            await self._put_state(
                tx,
                "work_runtime",
                work_id,
                runtime,
                expected_projection_revision=runtime_record["revision"],
            )
            operation_id = uuid5(
                NAMESPACE_URL,
                f"ai-nature-company:{company_id}:runtime.tick:lease:{work_id}:{expired_lease['lease_id']}",
            )
            await self._append_event(
                tx,
                operation_id=operation_id,
                suffix="work-lease-expired",
                aggregate_type="work_item",
                aggregate_id=work_id,
                event_type="work.lease_expired",
                principal=principal,
                payload={
                    "state": updated,
                    "expired_lease_id": expired_lease["lease_id"],
                    "requeued": updated.get("status") == WorkStatus.OPEN.value,
                },
                correlation_id=uuid5(NAMESPACE_URL, f"ai-nature-company:{company_id}:runtime.tick"),
                idempotency_key=f"runtime.tick:lease:{work_id}",
            )
            expired_ids.append(work_id)
        expired_ids.sort()
        return expired_ids

    async def _tick_close_due_experiments(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        company_id: str,
    ) -> tuple[list[str], list[str]]:
        evidence_types = await self._evidence_types_by_experiment(tx)
        closed_ids: list[str] = []
        closeout_ids: list[str] = []
        for record in await tx.list_projections(projection_type="experiment", limit=1_000):
            state = record["state"]
            experiment_id = str(state["experiment_id"])
            view = await self._experiment_bet_view(
                tx,
                state,
                evidence_types=evidence_types.get(experiment_id, ()),
            )
            if view["close_eligible"]:
                runtime = await tx.get_projection("experiment_runtime", experiment_id)
                recorded = 0
                if runtime is not None and runtime["state"].get("actual_cost_minor") is not None:
                    recorded = int(runtime["state"]["actual_cost_minor"])
                actual_cost = state.get("actual_cost")
                if isinstance(actual_cost, Mapping) and actual_cost.get("amount_minor") is not None:
                    recorded = int(actual_cost["amount_minor"])
                budget_minor = int(state["budget"]["amount_minor"])
                updated = await self._close_running_experiment(
                    tx,
                    principal,
                    experiment_id=experiment_id,
                    record=record,
                    actual_minor=min(recorded, budget_minor),
                    operation_id=uuid5(
                        NAMESPACE_URL,
                        f"ai-nature-company:{company_id}:runtime.tick:close:{experiment_id}",
                    ),
                    correlation_id=uuid5(
                        NAMESPACE_URL, f"ai-nature-company:{company_id}:runtime.tick"
                    ),
                    idempotency_key=f"runtime.tick:close:{experiment_id}",
                )
                closed_ids.append(experiment_id)
                created = await self._ensure_closeout_work(
                    tx,
                    principal,
                    company_id=company_id,
                    experiment=updated,
                )
                if created is not None:
                    closeout_ids.append(created)
            elif view["state"] == ExperimentStatus.EVALUATING.value:
                created = await self._ensure_closeout_work(
                    tx,
                    principal,
                    company_id=company_id,
                    experiment=state,
                )
                if created is not None:
                    closeout_ids.append(created)
        closed_ids.sort()
        closeout_ids.sort()
        return closed_ids, closeout_ids

    async def _close_running_experiment(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        *,
        experiment_id: str,
        record: Mapping[str, Any],
        actual_minor: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
        reason: str | None = None,
        founder_decision_ref: str | None = None,
    ) -> dict[str, Any]:
        state = dict(record["state"])
        expected_revision = int(state["revision"])
        if state.get("state") != ExperimentStatus.RUNNING.value:
            raise ValidationError("only a running experiment can close")
        runtime_record, _ = await self._projection(tx, "experiment_runtime", experiment_id)
        runtime = dict(runtime_record["state"])
        reservation_group_id = str(runtime.get("reservation_group_id", runtime["reservation_id"]))
        reservation = await tx.get_reservation_group(reservation_group_id)
        if reservation is None:
            raise NotFoundError("experiment reservation does not exist")
        experiment_unit, experiment_currency = identity_of(state["budget"])
        committed = await tx.commit_reservation_group(
            reservation_group_id=reservation_group_id,
            actual_amount=quantity_to_ledger(actual_minor, experiment_unit),
            expected_revisions={
                member["reservation_id"]: member["revision"] for member in reservation["members"]
            },
            now=self.clock(),
        )
        close_fields: dict[str, Any] = {}
        if reason is not None:
            close_fields["reason"] = reason
        if founder_decision_ref is not None:
            close_fields["founder_decision_ref"] = founder_decision_ref
        updated = self._revised_state(
            state,
            expected_revision,
            state=ExperimentStatus.EVALUATING.value,
            closed_at=_jsonable(self.clock()),
            actual_cost=dump_budget_amount(
                unit=experiment_unit,
                amount_minor=actual_minor,
                currency=experiment_currency,
            ),
            **close_fields,
        )
        await self._put_state(
            tx,
            "experiment",
            experiment_id,
            updated,
            expected_projection_revision=record["revision"],
        )
        runtime["actual_cost_minor"] = actual_minor
        await self._put_state(
            tx,
            "experiment_runtime",
            experiment_id,
            runtime,
            expected_projection_revision=runtime_record["revision"],
        )
        event_payload: dict[str, Any] = {"state": updated, "reservation": committed}
        if reason is not None:
            event_payload["reason"] = reason
        if founder_decision_ref is not None:
            event_payload["founder_decision_ref"] = founder_decision_ref
        await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="experiment-closed",
            aggregate_type="experiment",
            aggregate_id=experiment_id,
            event_type="experiment.closed_for_evaluation",
            principal=principal,
            payload=event_payload,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return updated

    async def _ensure_closeout_work(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        *,
        company_id: str,
        experiment: Mapping[str, Any],
    ) -> str | None:
        experiment_id = str(experiment["experiment_id"])
        work_item_id = str(uuid5(_uuid(experiment_id, "experiment_id"), "closeout-evaluation"))
        existing = await tx.get_projection("work_item", work_item_id)
        if existing is not None:
            return None
        at = self.clock()
        budget = experiment["budget"]
        work_budget: dict[str, Any] = {"maximum_cost_minor": 0}
        if budget.get("unit") is not None:
            work_budget["unit"] = budget["unit"]
        if budget.get("currency") is not None:
            work_budget["currency"] = budget["currency"]
        item = WorkItem.model_validate(
            {
                "work_item_id": work_item_id,
                "company_id": company_id,
                "cell_id": experiment["cell_id"],
                "experiment_id": experiment_id,
                "revision": 1,
                "status": WorkStatus.OPEN,
                "kind": "evaluate_experiment",
                "outcome_contract": OutcomeContract(
                    outcome="Submit an independent evaluation of the closed experiment.",
                    acceptance_criteria=(
                        "Evaluation cites frozen evidence and the preregistered thresholds.",
                        "Judge the time window against window_ends_at, not hypothesis prose.",
                    ),
                    result_schema_ref="schemas/evaluation-v1.json",
                ),
                "eligible_capabilities": frozenset({"evaluation.submit"}),
                "budget": WorkBudget.model_validate(work_budget),
                "approval_requirements": frozenset({ApprovalRequirement.NONE}),
                "input_references": (
                    ResourceReference(
                        kind="experiment",
                        id=_uuid(experiment_id, "experiment_id"),
                        revision=int(experiment["revision"]),
                    ),
                ),
                "max_attempts": 3,
                "created_by_principal_id": RUNTIME_TICK_PRINCIPAL_ID,
                "created_at": at,
            }
        )
        await self._put_model(
            tx, "work_item", item.work_item_id, item, expected_projection_revision=0
        )
        await self._put_state(
            tx,
            "work_runtime",
            item.work_item_id,
            {"attempt": 0, "lease_id": None, "result_evidence_ids": []},
            expected_projection_revision=0,
        )
        operation_id = uuid5(
            NAMESPACE_URL,
            f"ai-nature-company:{company_id}:runtime.tick:closeout:{experiment_id}",
        )
        await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="work-created",
            aggregate_type="work_item",
            aggregate_id=item.work_item_id,
            event_type="work.created",
            principal=principal,
            payload={"state": item},
            correlation_id=uuid5(NAMESPACE_URL, f"ai-nature-company:{company_id}:runtime.tick"),
            idempotency_key=f"runtime.tick:closeout:{experiment_id}",
        )
        return work_item_id

    async def _read_company_describe(
        self, principal: VerifiedPrincipal, payload: dict[str, Any]
    ) -> Mapping[str, Any]:
        if "cell_records" in payload:
            if set(payload) != {"cell_records"}:
                raise ValidationError("cell_records requests accept no other top-level fields")
            return await self._read_cell_records(principal, payload["cell_records"])
        company_id = _principal_company(principal)
        identity = principal.agent_identity
        resource = (
            ResourceScope.resource(company_id, ResourceType.CELL, identity.cell_id)
            if identity is not None and identity.cell_id is not None
            else ResourceScope.company(company_id)
        )
        self._require(principal, Capability.COMPANY_DESCRIBE, resource)
        async with self.repository.transaction(company_id) as tx:
            company_record, _ = await self._projection(tx, "company", company_id)
            constitution_id = company_record["state"]["active_constitution_id"]
            constitution_record, _ = await self._projection(tx, "constitution", constitution_id)
            roles = sorted(role.value for role in principal.roles)
            effective_roles = {identity.role} if identity is not None else principal.roles
            capabilities = {
                _public_capability_name(capability.value)
                for role in effective_roles
                for capability in ROLE_CAPABILITIES[role]
            }
            binding = await tx.get_projection("agent_binding", principal.principal_id)
            if binding is not None:
                instance = await self._binding(tx, principal)
                capabilities &= set(instance.get("allowed_actions", ()))
            principal_view: dict[str, Any] = {
                "principal_id": principal.principal_id,
                "roles": roles,
                "effective_capabilities": sorted(capabilities),
            }
            if identity is not None:
                principal_view.update(
                    {
                        "agent_instance_id": identity.agent_instance_id,
                        "definition_id": identity.definition_id,
                        "definition_version": identity.definition_version,
                        "role": identity.role.value,
                        "cell_id": identity.cell_id,
                        "agent_run_id": principal.agent_run_id,
                    }
                )
                run_view = principal.caller_agent_run_view(now=self.clock())
                if run_view["expires_at"] is not None:
                    principal_view["agent_run_expires_at"] = run_view["expires_at"]
                    principal_view["agent_run_expires_soon"] = run_view["expires_soon"]
            evidence_types = await self._evidence_types_by_experiment(tx)
            ledger_rows = await self._list_active_ledger_rows(tx)
            identity_cell = identity.cell_id if identity is not None else None
            experiment_records = await tx.list_projections(projection_type="experiment", limit=500)
            cells: list[dict[str, Any]] = []
            for cell_record in await tx.list_projections(projection_type="cell", limit=500):
                cell_state = cell_record["state"]
                cell_id = str(cell_state["cell_id"])
                if identity_cell is not None and cell_id != identity_cell:
                    continue
                bets = [
                    await self._experiment_bet_view(
                        tx,
                        experiment_record["state"],
                        evidence_types=evidence_types.get(
                            str(experiment_record["state"]["experiment_id"]), ()
                        ),
                    )
                    for experiment_record in experiment_records
                    if str(experiment_record["state"].get("cell_id")) == cell_id
                ]
                cell_ledgers = [row for row in ledger_rows if row.get("cell_id") == cell_id]
                counts = await self._cell_operator_counts(tx, cell_id)
                cells.append(
                    self._cell_bet_view(
                        cell_state,
                        experiments=bets,
                        ledger_rows=cell_ledgers,
                        **counts,
                    )
                )
            visible_ledgers = ledger_rows
            if identity_cell is not None:
                visible_ledgers = [
                    row
                    for row in ledger_rows
                    if row.get("scope") in {"company", "stage"}
                    or row.get("cell_id") == identity_cell
                ]
            return {
                "company": _jsonable(company_record["state"]),
                "constitution": _jsonable(constitution_record["state"]),
                "principal": principal_view,
                "portfolio": {
                    "remaining_budget_by_unit": visible_ledgers,
                    "cells": cells,
                },
            }

    async def _read_cell_records(
        self, principal: VerifiedPrincipal, query: object
    ) -> Mapping[str, Any]:
        """Resume work without chat state using already-declared discovery authority."""

        if not isinstance(query, dict) or set(query) - {"cell_id", "kind", "limit", "after_id"}:
            raise ValidationError("cell_records must contain only cell_id, kind, limit, after_id")
        cell_id = str(_uuid(query.get("cell_id"), "cell_id"))
        kind = query.get("kind")
        if not isinstance(kind, str) or kind not in CELL_RECORD_KINDS:
            raise ValidationError("unsupported cell record kind")
        limit = query.get("limit", 20)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_CELL_RECORDS
        ):
            raise ValidationError(f"cell record limit must be between 1 and {MAX_CELL_RECORDS}")
        after_id = query.get("after_id")
        if after_id is not None:
            after_id = str(_uuid(after_id, "after_id"))
        company_id = _principal_company(principal)
        identity = principal.agent_identity
        if identity is not None and identity.cell_id is not None and identity.cell_id != cell_id:
            raise AuthorizationError("agent identity does not cover the requested cell")
        self._require(
            principal,
            Capability.COMPANY_DESCRIBE,
            ResourceScope.resource(company_id, ResourceType.CELL, cell_id),
        )
        async with self.repository.transaction(company_id) as tx:
            # Preserve a stored cell ceiling for legacy non-HTTP bindings too.
            if await tx.get_projection("agent_binding", principal.principal_id) is not None:
                await self._assert_agent_capabilities(
                    tx, principal, {Capability.COMPANY_DESCRIBE.value}, cell_id=cell_id
                )
            await self._projection(tx, "cell", cell_id)
            records = await tx.list_projections(
                projection_type=kind,
                cell_id=cell_id,
                after_projection_id=after_id,
                limit=limit + 1,
            )
            page: list[dict[str, Any]] = []
            used_bytes = 0
            for record in records[:limit]:
                item_state = _jsonable(_redact_export_secrets(record["state"]))
                if kind == "work_item":
                    item_state = await self._observed_work_item(tx, item_state)
                item = {
                    "record_id": record["projection_id"],
                    "revision": (
                        int(record["state"]["version"]) - 1
                        if kind == "offer"
                        else record["state"].get("revision", record["revision"])
                    ),
                    "state": item_state,
                }
                if kind == "experiment" and record["state"].get("evaluation_id"):
                    evaluation, _ = await self._projection(
                        tx, "evaluation", record["state"]["evaluation_id"]
                    )
                    if str(evaluation["state"].get("experiment_id")) != record["projection_id"]:
                        raise ValidationError("linked evaluation does not match the experiment")
                    item["evaluation"] = _jsonable(_redact_export_secrets(evaluation["state"]))
                size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                if used_bytes + size > MAX_CELL_RECORD_BYTES:
                    if not page:
                        raise ValidationError("cell record exceeds the bounded read size")
                    break
                page.append(item)
                used_bytes += size
            return {
                "cell_records": {
                    "cell_id": cell_id,
                    "kind": kind,
                    "observed_at": _jsonable(self.clock()),
                    "records": page,
                    "next_after_id": (
                        page[-1]["record_id"] if len(records) > len(page) and page else None
                    ),
                }
            }

    async def _read_constitution_get(
        self, principal: VerifiedPrincipal, payload: dict[str, Any]
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        self._require(principal, Capability.CONSTITUTION_GET, ResourceScope.company(company_id))
        async with self.repository.transaction(company_id) as tx:
            company_record, _ = await self._projection(tx, "company", company_id)
            constitution_id = company_record["state"]["active_constitution_id"]
            constitution_record, _ = await self._projection(tx, "constitution", constitution_id)
            return {"constitution": _jsonable(constitution_record["state"])}

    async def _read_protocol_describe(
        self, principal: VerifiedPrincipal, payload: dict[str, Any]
    ) -> Mapping[str, Any]:
        del payload
        from .server import PHASE_ONE_TOOL_NAMES

        company_id = _principal_company(principal)
        self._require(principal, Capability.PROTOCOL_DESCRIBE, ResourceScope.company(company_id))
        return {
            "protocol_version": 1,
            "caller_agent_run": principal.caller_agent_run_view(now=self.clock()),
            "tools": list(PHASE_ONE_TOOL_NAMES),
            "mutation_contract": [
                "idempotency_key",
                "expected_revision",
                "correlation_id",
                "operation_intent",
                "payload",
            ],
            "inference_inside_substrate": False,
            "agent_scheduler_inside_substrate": False,
            "budget_units": ["money", "time", "cycles", "tries"],
            "time_scale": "seconds",
            "natural_forces": describe_natural_forces(),
            "runtime_tick": {
                "entrypoint": "ai-nature-tick",
                "inference_inside_substrate": False,
                "agent_scheduler_inside_substrate": False,
                "forces": ["maintain", "continue"],
                "maintain": ["expired_work_leases", "overdue_open_work"],
                "continue": ["due_experiment_closeout", "closeout_evaluation_work"],
                "advances": [
                    "expired_work_leases",
                    "overdue_open_work",
                    "due_experiment_closeout",
                    "closeout_evaluation_work",
                ],
                "not": "reflex",
                "scheduled": None,
                "on_demand": {
                    "tool": "maintenance.run",
                    "authority": ["founder (company or one cell)", "cell_owner (own cell)"],
                    "payload": {"cell_id?": "string", "end_runs_on_killed_cells?": "boolean"},
                    "state_mutations": (
                        "none, unless end_runs_on_killed_cells ends runs still running on "
                        "killed cells (idempotency key + audit row)"
                    ),
                },
                "cell_kill_ends_agent_runs": (
                    "cell.transition to killed (founder or owner-signed) ends every running "
                    "agent run of the cell in the same transaction; the audit row lists "
                    "ended_agent_run_ids"
                ),
            },
            "cell_routines": {name: dict(spec) for name, spec in CELL_ROUTINES.items()},
            "effectors": describe_effectors(),
            "signed_cast": [
                "market-scout",
                "offer-designer",
                "sales",
                "delivery",
                "experiment-evaluator",
                "memory",
                "allocator",
            ],
            "signed_cast_are": "optional_effectors_inside_forces",
            "bet_workflow": describe_bet_workflow(),
        }

    async def _read_agent_get(
        self, principal: VerifiedPrincipal, payload: dict[str, Any]
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        self._require(principal, Capability.AGENT_GET, ResourceScope.company(company_id))
        agent_id = payload.get("agent_instance_id", payload.get("agent_id"))
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValidationError("agent_instance_id is required")
        async with self.repository.transaction(company_id) as tx:
            record, _ = await self._projection(tx, "agent", agent_id.strip())
            identity = principal.agent_identity
            if identity is not None and identity.cell_id is not None:
                target_cell = record["state"].get("cell_id")
                if target_cell is None or str(target_cell) != identity.cell_id:
                    raise AuthorizationError("agent identity does not cover the requested cell")
            return {"agent": _jsonable(record["state"])}

    async def _read_agent_list(
        self, principal: VerifiedPrincipal, payload: dict[str, Any]
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        self._require(principal, Capability.AGENT_LIST, ResourceScope.company(company_id))
        cell_id = payload.get("cell_id")
        identity = principal.agent_identity
        if identity is not None and identity.cell_id is not None:
            if cell_id is not None and str(cell_id) != identity.cell_id:
                raise AuthorizationError("agent identity does not cover the requested cell")
            cell_id = identity.cell_id
        async with self.repository.transaction(company_id) as tx:
            records = await tx.list_projections(projection_type="agent", limit=500)
            agents = [
                _jsonable(record["state"])
                for record in records
                if cell_id is None or record["state"].get("cell_id") == str(cell_id)
            ]
            return {"agents": agents}

    async def _read_cell_get(
        self, principal: VerifiedPrincipal, payload: dict[str, Any]
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        cell_id = _required_str(payload, "cell_id")
        identity = principal.agent_identity
        if identity is not None and identity.cell_id is not None and identity.cell_id != cell_id:
            raise AuthorizationError("agent identity does not cover the requested cell")
        self._require(
            principal,
            Capability.CELL_GET,
            ResourceScope.resource(company_id, ResourceType.CELL, cell_id),
        )
        async with self.repository.transaction(company_id) as tx:
            record, _ = await self._projection(tx, "cell", cell_id)
            evidence_types = await self._evidence_types_by_experiment(tx)
            ledger_rows = [
                row
                for row in await self._list_active_ledger_rows(tx)
                if row.get("cell_id") == cell_id
            ]
            experiments = [
                await self._experiment_bet_view(
                    tx,
                    experiment_record["state"],
                    evidence_types=evidence_types.get(
                        str(experiment_record["state"]["experiment_id"]), ()
                    ),
                )
                for experiment_record in await tx.list_projections(
                    projection_type="experiment", limit=500
                )
                if str(experiment_record["state"].get("cell_id")) == cell_id
            ]
            counts = await self._cell_operator_counts(tx, cell_id)
            bet = self._cell_bet_view(
                record["state"],
                experiments=experiments,
                ledger_rows=ledger_rows,
                **counts,
            )
            work_items = await self._cell_work_summaries(tx, cell_id)
            return {
                "cell": _jsonable(record["state"]),
                "experiments": bet["experiments"],
                "eligibility": bet["eligibility"],
                "remaining_budget_minor": bet["remaining_budget_minor"],
                "spent_minor": bet["spent_minor"],
                "activity_evidence_count": bet["activity_evidence_count"],
                "fitness_evidence_count": bet["fitness_evidence_count"],
                "stale_open_work_count": bet["stale_open_work_count"],
                "work_items": work_items,
            }

    async def _read_cell_list(
        self, principal: VerifiedPrincipal, payload: dict[str, Any]
    ) -> Mapping[str, Any]:
        del payload
        company_id = _principal_company(principal)
        identity = principal.agent_identity
        resource = (
            ResourceScope.resource(company_id, ResourceType.CELL, identity.cell_id)
            if identity is not None and identity.cell_id is not None
            else ResourceScope.company(company_id)
        )
        self._require(principal, Capability.CELL_LIST, resource)
        async with self.repository.transaction(company_id) as tx:
            records = await tx.list_projections(projection_type="cell", limit=500)
            return {
                "cells": [
                    _jsonable(record["state"])
                    for record in records
                    if identity is None
                    or identity.cell_id is None
                    or record["state"].get("cell_id") == identity.cell_id
                ]
            }

    async def _read_audit_list(
        self, principal: VerifiedPrincipal, payload: dict[str, Any]
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        self._require(principal, Capability.AUDIT_LIST, ResourceScope.company(company_id))
        after_position = payload.get("after_position", 0)
        limit = payload.get("limit", 100)
        if isinstance(after_position, bool) or not isinstance(after_position, int):
            raise ValidationError("after_position must be a non-negative integer")
        if after_position < 0:
            raise ValidationError("after_position must not be negative")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValidationError("limit must be a positive integer")
        limit = min(limit, 1000)
        async with self.repository.transaction(company_id) as tx:
            binding_cell: str | None = None
            binding_record = await tx.get_projection("agent_binding", principal.principal_id)
            if binding_record is not None:
                raw_cell = binding_record["state"].get("cell_id")
                if raw_cell:
                    binding_cell = str(raw_cell)
                await self._assert_agent_capabilities(
                    tx,
                    principal,
                    {Capability.AUDIT_LIST.value},
                    cell_id=binding_cell,
                    require_bound_cell_target=binding_cell is not None,
                )
            scoped_cell = self._audit_list_cell_scope(principal, binding_cell)
            records = await tx.list_audit_events(
                after_position=after_position,
                limit=1000 if scoped_cell is not None else limit,
            )
            if scoped_cell is not None:
                work_item_cells = await self._work_item_cell_index(tx)
                records = [
                    event
                    for event in records
                    if _resolved_audit_event_cell_id(event, work_item_cells) == scoped_cell
                ][:limit]
            return {"audit_events": _jsonable(records)}

    @staticmethod
    def _audit_list_cell_scope(
        principal: VerifiedPrincipal,
        binding_cell: str | None,
    ) -> str | None:
        """Company-wide roles see every audit row. Cell owners stay cell-scoped."""

        if any(grant.role in _COMPANY_WIDE_AUDIT_ROLES for grant in principal.grants):
            return None
        identity_cell = (
            principal.agent_identity.cell_id if principal.agent_identity is not None else None
        )
        scoped = identity_cell or binding_cell
        if scoped is None:
            raise AuthorizationError("audit.list is cell-scoped for this principal")
        return str(scoped)

    async def _read_decision_explain(
        self, principal: VerifiedPrincipal, payload: dict[str, Any]
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        self._require(principal, Capability.DECISION_EXPLAIN, ResourceScope.company(company_id))
        decision_id = _required_str(payload, "decision_id")
        async with self.repository.transaction(company_id) as tx:
            record, _ = await self._projection(tx, "decision", decision_id)
            return {"decision": _jsonable(record["state"])}

    async def _mutate_company_bootstrap(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        self.policy.require(
            principal,
            Capability.AMEND_CONSTITUTION,
            ResourceScope.company(company_id),
        )
        if expected_revision != 0:
            raise ConflictError("company bootstrap requires expected_revision 0")
        claimed_company = payload.get("company_id")
        if claimed_company is not None and str(claimed_company) != company_id:
            raise AuthorizationError("company payload does not match verified scope")
        if await tx.get_projection("company", company_id) is not None:
            raise ConflictError("company is already bootstrapped")

        at = self.clock()
        company_uuid = _uuid(company_id, "company_id")
        founder_uuid = _principal_uuid(principal)
        constitution_payload = payload.get("constitution")
        budget_payload = payload.get("budget")
        agents_payload = payload.get("agents", [])
        if not isinstance(constitution_payload, Mapping):
            raise ValidationError("constitution is required")
        if not isinstance(budget_payload, Mapping):
            raise ValidationError("budget is required")
        if not isinstance(agents_payload, list):
            raise ValidationError("agents must be a list")

        constitution_values = dict(constitution_payload)
        constitution_values.update(
            company_id=company_uuid,
            status=ConstitutionStatus.ACTIVE,
            created_at=at,
            effective_at=at,
            created_by_principal_id=founder_uuid,
        )
        constitution_values.setdefault("constitution_id", uuid5(operation_id, "constitution"))
        # Preserve the small bootstrap compatibility contract while materializing
        # the full canonical constitution on the server.
        if "mission" not in constitution_values:
            purpose = _required_str(constitution_payload, "purpose")
            objective = _required_str(constitution_payload, "objective")
            constitution_values["mission"] = f"{purpose} {objective}"
        constitution_values.setdefault("version", 1)
        policy_hint = constitution_values.get("budget_policy")
        default_unit = MONEY
        if isinstance(policy_hint, Mapping):
            default_unit = str(policy_hint.get("unit") or MONEY)
        elif isinstance(policy_hint, BudgetPolicy):
            default_unit = policy_hint.unit.value
        declared_unit, declared_currency = identity_of(budget_payload, default_unit=default_unit)
        company_limit_minor = parse_limit_to_minor(
            budget_payload.get("limit"), declared_unit, "budget.limit"
        )
        founder_actions = constitution_payload.get(
            "founder_reserved_actions",
            ["company.dissolve", "constitution.activate"],
        )
        constitution_values.setdefault(
            "authority_policy",
            AuthorityPolicy(
                founder_principal_ids=frozenset({founder_uuid}),
                irreversible_actions=frozenset(founder_actions),
                human_approval_actions=frozenset({"constitution.activate"}),
            ),
        )
        constitution_values.setdefault(
            "risk_policy",
            RiskPolicy(
                prohibited_actions=frozenset({"claim.fabricate"}),
                pricing_floor_minor=0,
                max_external_messages_per_experiment=100,
                regulated_market_entry_requires_founder=True,
            ),
        )
        constitution_values.setdefault(
            "budget_policy",
            BudgetPolicy(
                unit=declared_unit,
                currency=declared_currency,
                company_limit_minor=company_limit_minor,
                cell_limit_minor=company_limit_minor,
                experiment_limit_minor=company_limit_minor,
                single_action_limit_minor=company_limit_minor,
                exploration_share_bps=2_000,
            ),
        )
        constitution_values.setdefault(
            "fitness_policy",
            FitnessPolicy(
                required_gates=frozenset(GateName),
                minimum_evidence_count=1,
                minimum_customer_outcome_bps=7_000,
                minimum_delivery_quality_bps=8_000,
                allocation_cadence_days=7,
            ),
        )
        constitution_values.pop("purpose", None)
        constitution_values.pop("objective", None)
        constitution_values.pop("principles", None)
        constitution_values.pop("founder_reserved_actions", None)
        constitution = Constitution.model_validate(constitution_values)
        if not same_ledger_identity(
            {
                "unit": constitution.budget_policy.unit.value,
                "currency": constitution.budget_policy.currency,
            },
            {"unit": declared_unit, "currency": declared_currency},
        ):
            raise ValidationError("bootstrap budget unit must match the constitution budget policy")
        if company_limit_minor != constitution.budget_policy.company_limit_minor:
            raise ValidationError(
                "bootstrap budget limit must match the constitution company limit"
            )
        constitution_id = constitution.constitution_id
        company = Company(
            company_id=company_uuid,
            name=_required_str(payload, "name"),
            founder_principal_id=founder_uuid,
            active_constitution_id=constitution_id,
            created_at=at,
            updated_at=at,
        )
        await self._put_model(
            tx,
            "constitution",
            constitution_id,
            constitution,
            expected_projection_revision=0,
        )
        await self._put_model(
            tx,
            "company",
            company_uuid,
            company,
            expected_projection_revision=0,
        )
        budget_id = _uuid(
            budget_payload.get("budget_id", uuid5(operation_id, "company-budget")),
            "budget_id",
        )
        exploration_budget_id = uuid5(budget_id, "stage:exploration")
        exploitation_budget_id = uuid5(budget_id, "stage:exploitation")
        budget_ends_at = at + timedelta(days=int(budget_payload.get("duration_days", 365)))
        budget = await tx.create_budget(
            budget_id=str(budget_id),
            scope=BudgetScope.COMPANY.value,
            limit=quantity_to_ledger(company_limit_minor, declared_unit),
            unit=declared_unit,
            currency=declared_currency,
            starts_at=at,
            ends_at=budget_ends_at,
            metadata={
                "ledger_kind": "constraint",
                "stage_budget_ids": {
                    AllocationStage.EXPLORATION.value: str(exploration_budget_id),
                    AllocationStage.EXPLOITATION.value: str(exploitation_budget_id),
                },
            },
            now=at,
        )
        exploration_limit_minor = (
            company_limit_minor * constitution.budget_policy.exploration_share_bps // 10_000
        )
        stage_budgets: dict[str, dict[str, Any]] = {}
        for stage, stage_budget_id, stage_limit_minor in (
            (
                AllocationStage.EXPLORATION.value,
                exploration_budget_id,
                exploration_limit_minor,
            ),
            (
                AllocationStage.EXPLOITATION.value,
                exploitation_budget_id,
                company_limit_minor - exploration_limit_minor,
            ),
        ):
            stage_budgets[stage] = await tx.create_budget(
                budget_id=str(stage_budget_id),
                scope=STAGE_BUDGET_SCOPE,
                parent_budget_id=str(budget_id),
                stage=stage,
                limit=quantity_to_ledger(stage_limit_minor, declared_unit),
                unit=declared_unit,
                currency=declared_currency,
                starts_at=at,
                ends_at=budget_ends_at,
                metadata={"ledger_kind": "constraint", "stage": stage},
                now=at,
            )

        agents: list[dict[str, Any]] = []
        event_ids: list[str] = []
        event_ids.append(
            await self._append_event(
                tx,
                operation_id=operation_id,
                suffix="company",
                aggregate_type="company",
                aggregate_id=company_uuid,
                event_type="company.bootstrapped",
                principal=principal,
                payload={"state": company},
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
        )
        event_ids.append(
            await self._append_event(
                tx,
                operation_id=operation_id,
                suffix="constitution",
                aggregate_type="constitution",
                aggregate_id=constitution_id,
                event_type="constitution.activated",
                principal=principal,
                payload={"state": constitution},
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
        )
        for index, raw in enumerate(agents_payload):
            if not isinstance(raw, Mapping):
                raise ValidationError("each agent must be an object")
            definition_payload = raw.get("definition")
            if isinstance(definition_payload, Mapping):
                definition = AgentDefinition.model_validate(definition_payload)
                canonical_definition = await self._ensure_agent_definition(tx, definition)
                definition_id = definition.definition_id
                agent_instance_id = _agent_instance_id(raw, definition_id)
                role = _agent_instance_role(raw, definition)
                capabilities = sorted(definition.declared_capabilities)
                description = definition.description
                definition_version = definition.version
                name = definition.name
            else:
                definition_id = _uuid(
                    raw.get("agent_id", uuid5(operation_id, f"agent-{index}")),
                    "agent_id",
                )
                agent_instance_id = _uuid(
                    raw.get("agent_instance_id", raw.get("agent_id", definition_id)),
                    "agent_instance_id",
                )
                role = _required_str(raw, "role")
                capabilities = sorted(set(raw.get("capabilities", ())))
                description = raw.get("description")
                definition_version = int(raw.get("definition_version", 1))
                name = _required_str(raw, "name")
                canonical_definition = None
            target_principal = _required_str(raw, "principal_id")
            allowed_actions = sorted(set(raw.get("allowed_actions", capabilities)))
            if canonical_definition is not None and not set(allowed_actions) <= set(capabilities):
                raise ValidationError(
                    "agent instance allowed_actions exceed its definition declaration"
                )
            agent_view = {
                **(canonical_definition or {}),
                "agent_instance_id": str(agent_instance_id),
                "agent_id": str(agent_instance_id),
                "definition_id": str(definition_id),
                "definition_version": definition_version,
                "company_id": company_id,
                "principal_id": target_principal,
                "cell_id": str(raw["cell_id"]) if raw.get("cell_id") else None,
                "name": name,
                "role": role,
                "capabilities": capabilities,
                "allowed_actions": allowed_actions,
                "description": description,
                "status": "active",
                "definition": canonical_definition,
            }
            await self._put_state(
                tx, "agent", agent_instance_id, agent_view, expected_projection_revision=0
            )
            await tx.put_projection(
                projection_type="agent_binding",
                projection_id=target_principal,
                state={
                    "principal_id": target_principal,
                    "agent_instance_id": str(agent_instance_id),
                    "agent_id": str(agent_instance_id),
                    "definition_id": str(definition_id),
                    "definition_version": definition_version,
                    "role": role,
                    "cell_id": agent_view["cell_id"],
                    "status": "active",
                },
                expected_revision=0,
                updated_at=at,
            )
            agents.append(agent_view)
            event_ids.append(
                await self._append_event(
                    tx,
                    operation_id=operation_id,
                    suffix=f"agent-{index}",
                    aggregate_type="agent",
                    aggregate_id=agent_instance_id,
                    event_type="agent.registered",
                    principal=principal,
                    payload={"state": agent_view, "principal_id": target_principal},
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                )
            )
        return {
            "company": company,
            "constitution": constitution,
            "agents": agents,
            "budget": budget,
            "stage_budgets": stage_budgets,
            "_event_ids": event_ids,
            "_prior_revision": 0,
            "_new_revision": 1,
            "_resource_type": "company",
            "_resource_id": company_id,
        }

    async def _mutate_constitution_amend(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        self.policy.require(
            principal,
            Capability.AMEND_CONSTITUTION,
            ResourceScope.company(company_id),
        )
        await self._assert_agent_capabilities(tx, principal, {Capability.AMEND_CONSTITUTION.value})
        allowed_keys = {
            "budget_policy",
            "mission",
            "risk_policy",
            "fitness_policy",
            "expected_revision",
        }
        unknown = set(payload) - allowed_keys
        if unknown:
            raise ValidationError("constitution.amend payload contains unsupported fields")
        if "expected_revision" in payload and payload["expected_revision"] != expected_revision:
            raise ValidationError("payload expected_revision must match the mutation envelope")
        raw_policy = payload.get("budget_policy")
        if not isinstance(raw_policy, Mapping):
            raise ValidationError("budget_policy is required")
        budget_policy = BudgetPolicy.model_validate(raw_policy)
        mission = _required_str(payload, "mission") if "mission" in payload else None
        risk_policy = (
            RiskPolicy.model_validate(payload["risk_policy"]) if "risk_policy" in payload else None
        )
        fitness_policy = (
            FitnessPolicy.model_validate(payload["fitness_policy"])
            if "fitness_policy" in payload
            else None
        )
        if fitness_policy is not None and fitness_policy.rule_version != "fitness-v1":
            raise ValidationError("fitness_policy.rule_version must remain fitness-v1")

        company_record, company = await self._projection(tx, "company", company_id, Company)
        assert isinstance(company, Company)
        if company.status is CompanyStatus.DISSOLVED:
            raise ValidationError("a dissolved company cannot amend its constitution")
        constitution_record, current = await self._projection(
            tx,
            "constitution",
            company.active_constitution_id,
            Constitution,
        )
        assert isinstance(current, Constitution)
        at = self.clock()
        amended = amend_constitution(
            current,
            expected_version=expected_revision,
            at=at,
            founder_authorized=True,
            created_by_principal_id=_principal_uuid(principal),
            budget_policy=budget_policy,
            mission=mission,
            risk_policy=risk_policy,
            fitness_policy=fitness_policy,
        )
        company_changed = amended.constitution_id != current.constitution_id
        if company_changed:
            superseded = current.model_copy(
                update={
                    "status": ConstitutionStatus.SUPERSEDED,
                    "supersedes_constitution_id": amended.constitution_id,
                }
            )
            await self._put_model(
                tx,
                "constitution",
                current.constitution_id,
                superseded,
                expected_projection_revision=constitution_record["revision"],
            )
            await self._put_model(
                tx,
                "constitution",
                amended.constitution_id,
                amended,
                expected_projection_revision=0,
            )
            updated_company = company.model_copy(
                update={
                    "active_constitution_id": amended.constitution_id,
                    "revision": company.revision + 1,
                    "updated_at": at,
                }
            )
            await self._put_model(
                tx,
                "company",
                company_id,
                updated_company,
                expected_projection_revision=company_record["revision"],
            )
            company = updated_company
        else:
            await self._put_model(
                tx,
                "constitution",
                amended.constitution_id,
                amended,
                expected_projection_revision=constitution_record["revision"],
            )

        ledgers = await self._realign_constraint_ledgers(
            tx,
            company_id=company_id,
            operation_id=operation_id,
            budget_policy=amended.budget_policy,
            at=at,
        )
        event_ids = [
            await self._append_event(
                tx,
                operation_id=operation_id,
                suffix="constitution-superseded",
                aggregate_type="constitution",
                aggregate_id=current.constitution_id,
                event_type="constitution.superseded",
                principal=principal,
                payload={
                    "constitution_id": current.constitution_id,
                    "version": current.version,
                    "replacement_constitution_id": amended.constitution_id,
                    "replacement_version": amended.version,
                    "budget_policy": current.budget_policy,
                },
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            ),
            await self._append_event(
                tx,
                operation_id=operation_id,
                suffix="constitution-activated",
                aggregate_type="constitution",
                aggregate_id=amended.constitution_id,
                event_type="constitution.activated",
                principal=principal,
                payload={"state": amended},
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            ),
            await self._append_event(
                tx,
                operation_id=operation_id,
                suffix="ledgers-realigned",
                aggregate_type="company",
                aggregate_id=company.company_id,
                event_type="constraint_ledgers.realigned",
                principal=principal,
                payload={
                    "from_unit": current.budget_policy.unit.value,
                    "from_currency": current.budget_policy.currency,
                    "to_unit": amended.budget_policy.unit.value,
                    "to_currency": amended.budget_policy.currency,
                    "closed_budget_ids": [row["budget_id"] for row in ledgers["closed"]],
                    "active_budget_ids": [row["budget_id"] for row in ledgers["active"]],
                },
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            ),
        ]
        return {
            "constitution": amended,
            "company": company,
            "ledgers": {
                "unit": ledgers["unit"],
                "currency": ledgers["currency"],
                "realigned": ledgers["realigned"],
                "closed_budget_ids": [row["budget_id"] for row in ledgers["closed"]],
                "active": [_budget_snapshot(row) for row in ledgers["active"]],
            },
            "_event_ids": event_ids,
            "_prior_revision": current.version,
            "_new_revision": amended.version,
            "_resource_type": "constitution",
            "_resource_id": amended.constitution_id,
        }

    async def _mutate_agent_register(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        self._require(principal, Capability.AGENT_REGISTER, ResourceScope.company(company_id))
        if expected_revision != 0:
            raise ConflictError("new agent registration requires expected_revision 0")
        at = self.clock()
        definition_payload = payload.get("definition", payload)
        if not isinstance(definition_payload, Mapping):
            raise ValidationError("definition must be an object")
        definition = AgentDefinition.model_validate(definition_payload)
        canonical_definition = await self._ensure_agent_definition(tx, definition)
        agent_instance_id = _agent_instance_id(payload, definition.definition_id)
        target_principal = _required_str(payload, "principal_id")
        role = _agent_instance_role(payload, definition)
        capabilities = sorted(definition.declared_capabilities)
        allowed_actions = sorted(set(payload.get("allowed_actions", capabilities)))
        if not set(allowed_actions) <= set(capabilities):
            raise ValidationError(
                "agent instance allowed_actions exceed its definition declaration"
            )
        agent_view = {
            **canonical_definition,
            "agent_instance_id": str(agent_instance_id),
            "agent_id": str(agent_instance_id),
            "definition_id": str(definition.definition_id),
            "definition_version": definition.version,
            "company_id": company_id,
            "principal_id": target_principal,
            "cell_id": str(payload["cell_id"]) if payload.get("cell_id") else None,
            "role": role,
            "capabilities": capabilities,
            "allowed_actions": allowed_actions,
            "status": "active",
            "definition": canonical_definition,
        }
        await self._put_state(
            tx,
            "agent",
            agent_instance_id,
            agent_view,
            expected_projection_revision=0,
        )
        binding_state = {
            "principal_id": target_principal,
            "agent_instance_id": str(agent_instance_id),
            "agent_id": str(agent_instance_id),
            "definition_id": str(definition.definition_id),
            "definition_version": definition.version,
            "role": role,
            "cell_id": agent_view["cell_id"],
            "status": "active",
        }
        await tx.put_projection(
            projection_type="agent_binding",
            projection_id=target_principal,
            state=binding_state,
            expected_revision=0,
            updated_at=at,
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="agent",
            aggregate_type="agent",
            aggregate_id=agent_instance_id,
            event_type="agent.registered",
            principal=principal,
            payload={"state": agent_view, "principal_id": target_principal},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "agent": agent_view,
            "binding": binding_state,
            "_event_ids": [event_id],
            "_prior_revision": 0,
            "_new_revision": 1,
            "_resource_type": "agent",
            "_resource_id": agent_instance_id,
        }

    async def _mutate_work_create(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        explicit_cell_id = (
            _required_str(payload, "cell_id") if payload.get("cell_id") is not None else None
        )
        binding = await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.WORK_CREATE.value},
            cell_id=explicit_cell_id,
        )
        experiment_id = (
            _required_str(payload, "experiment_id")
            if payload.get("experiment_id") is not None
            else None
        )
        experiment_record: dict[str, Any] | None = None
        experiment_cell_id: str | None = None
        if experiment_id is not None:
            experiment_record, _ = await self._projection(tx, "experiment", experiment_id)
            if experiment_record["state"].get("state") != ExperimentStatus.RUNNING.value:
                raise ValidationError("work requires a running experiment")
            experiment_cell_id = _required_str(experiment_record["state"], "cell_id")
            if explicit_cell_id is not None and experiment_cell_id != explicit_cell_id:
                raise ValidationError("work and experiment must belong to the same cell")
        cell_id = explicit_cell_id or experiment_cell_id
        bound_cell = binding.get("cell_id")
        if bound_cell is not None and (cell_id is None or str(bound_cell) != cell_id):
            raise AuthorizationError("agent binding does not cover this work cell")
        resource = (
            ResourceScope.resource(company_id, ResourceType.CELL, cell_id)
            if cell_id is not None
            else ResourceScope.company(company_id)
        )
        work_create_capability = Capability.WORK_CREATE
        if not self.policy.is_allowed(principal, work_create_capability, resource):
            work_create_capability = Capability.WORK_CREATE_ADMIN
            self._require(principal, work_create_capability, ResourceScope.company(company_id))
        if expected_revision != 0:
            raise ConflictError("new work requires expected_revision 0")
        at = self.clock()
        if cell_id is not None:
            _, cell = await self._projection(tx, "cell", cell_id, OfferCell)
            assert isinstance(cell, OfferCell)
            if cell.status is not CellStatus.ACTIVE:
                raise ValidationError("work can only be created for an active cell")
        values = dict(payload)
        values.pop("cancellation_history", None)
        for name in WORK_CLAIM_OBSERVATION_FIELDS:
            values.pop(name, None)
        values.update(
            work_item_id=_uuid(
                payload.get("work_item_id", uuid5(operation_id, "work-item")),
                "work_item_id",
            ),
            company_id=_uuid(company_id, "company_id"),
            revision=1,
            status=WorkStatus.OPEN,
            created_by_principal_id=_principal_uuid(principal),
            created_at=at,
            cell_id=cell_id,
            experiment_id=experiment_id,
        )
        try:
            item = WorkItem.model_validate(values)
        except PydanticValidationError as exc:
            raise _named_pydantic_error(exc) from exc
        if item.kind in REFLEX_WORK_KINDS:
            forbidden = item.eligible_capabilities & REFLEX_FORBIDDEN_CAPABILITIES
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise ValidationError(f"reflex work {item.kind} cannot include {names}")
        company_record, _ = await self._projection(tx, "company", company_id)
        constitution_record, _ = await self._projection(
            tx,
            "constitution",
            company_record["state"]["active_constitution_id"],
        )
        _require_matching_unit(
            item.budget.model_dump(mode="json"),
            constitution_record["state"]["budget_policy"],
            "work budget unit must match the constitution budget policy",
        )
        await self._put_model(
            tx, "work_item", item.work_item_id, item, expected_projection_revision=0
        )
        await self._put_state(
            tx,
            "work_runtime",
            item.work_item_id,
            {"attempt": 0, "lease_id": None, "result_evidence_ids": []},
            expected_projection_revision=0,
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="work-created",
            aggregate_type="work_item",
            aggregate_id=item.work_item_id,
            event_type="work.created",
            principal=principal,
            payload={"state": item},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "work_item": item,
            "_event_ids": [event_id],
            "_prior_revision": 0,
            "_new_revision": 1,
            "_resource_type": "work_item",
            "_resource_id": item.work_item_id,
        }

    async def _mutate_work_claim(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        work_id = _required_str(payload, "work_item_id")
        resource = ResourceScope.resource(company_id, ResourceType.WORK_ITEM, work_id)
        self._require(principal, Capability.WORK_CLAIM, resource)
        record, _ = await self._projection(tx, "work_item", work_id)
        state = dict(record["state"])
        if state.get("revision") != expected_revision:
            raise ConflictError("stale work item revision")
        cell_id = state.get("cell_id")
        required = set(state.get("eligible_capabilities", ())) | {Capability.WORK_CLAIM.value}
        await self._assert_agent_capabilities(
            tx,
            principal,
            required,
            cell_id=str(cell_id) if cell_id else None,
            require_bound_cell_target=True,
        )
        now = self.clock()
        runtime_record, _ = await self._projection(tx, "work_runtime", work_id)
        runtime = dict(runtime_record["state"])
        attempt = int(runtime.get("attempt", 0))
        prior_lease = await tx.get_lease(work_id, include_inactive=True, now=now)
        expired_lease: dict[str, Any] | None = None
        if state.get("status") == WorkStatus.CLAIMED.value:
            if (
                prior_lease is None
                or prior_lease.get("status") != "active"
                or prior_lease.get("expires_at") > now
            ):
                raise LeaseError("work item is not open")
            expired_lease = await tx.expire_lease(
                work_item_id=work_id,
                expected_revision=prior_lease["revision"],
                now=now,
            )
            runtime.update(lease_id=None)
            if attempt >= int(state["max_attempts"]):
                updated = self._revised_state(
                    state,
                    expected_revision,
                    status=WorkStatus.FAILED.value,
                )
                await self._put_state(
                    tx,
                    "work_item",
                    work_id,
                    updated,
                    expected_projection_revision=record["revision"],
                )
                runtime["failure_code"] = "lease_expired"
                await self._put_state(
                    tx,
                    "work_runtime",
                    work_id,
                    runtime,
                    expected_projection_revision=runtime_record["revision"],
                )
                event_id = await self._append_event(
                    tx,
                    operation_id=operation_id,
                    suffix="work-lease-expired",
                    aggregate_type="work_item",
                    aggregate_id=work_id,
                    event_type="work.lease_expired",
                    principal=principal,
                    payload={
                        "state": updated,
                        "expired_lease_id": expired_lease["lease_id"],
                        "failure_code": "lease_expired",
                    },
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                )
                return {
                    "work_item": updated,
                    "lease": None,
                    "reconciled_expiry": True,
                    "_event_ids": [event_id],
                    "_prior_revision": expected_revision,
                    "_new_revision": expected_revision + 1,
                    "_resource_type": "work_item",
                    "_resource_id": work_id,
                }
        elif state.get("status") != WorkStatus.OPEN.value:
            raise LeaseError("work item is not open")
        if cell_id is not None:
            cell_record, _ = await self._projection(tx, "cell", cell_id)
            cell_status = cell_record["state"].get("status", cell_record["state"].get("state"))
            if cell_status != CellStatus.ACTIVE.value:
                raise ValidationError("work claims are blocked while the cell is not active")
        run_id = str(self._principal_run_uuid(principal))
        if attempt >= int(state["max_attempts"]):
            raise LeaseError("work item has reached its maximum attempts")
        seconds = _lease_seconds(payload.get("lease_seconds", 300))
        lease_id = str(uuid5(operation_id, "work-lease"))
        lease = await tx.acquire_lease(
            work_item_id=work_id,
            agent_id=principal.principal_id,
            agent_run_id=run_id,
            ttl=seconds,
            lease_id=lease_id,
            cell_id=cell_id,
            expected_revision=(
                expired_lease["revision"]
                if expired_lease is not None
                else prior_lease["revision"]
                if prior_lease is not None
                else 0
            ),
            now=now,
        )
        updated = self._revised_state(state, expected_revision, status=WorkStatus.CLAIMED.value)
        await self._put_state(
            tx,
            "work_item",
            work_id,
            updated,
            expected_projection_revision=record["revision"],
        )
        runtime.update(attempt=attempt + 1, lease_id=lease_id)
        await self._put_state(
            tx,
            "work_runtime",
            work_id,
            runtime,
            expected_projection_revision=runtime_record["revision"],
        )
        lease_view = {
            **lease,
            "principal_id": lease["agent_id"],
            "agent_run_id": run_id,
        }
        event_ids: list[str] = []
        if expired_lease is not None:
            event_ids.append(
                await self._append_event(
                    tx,
                    operation_id=operation_id,
                    suffix="work-lease-expired",
                    aggregate_type="work_item",
                    aggregate_id=work_id,
                    event_type="work.lease_expired",
                    principal=principal,
                    payload={
                        "expired_lease_id": expired_lease["lease_id"],
                        "requeued": True,
                    },
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                )
            )
        event_ids.append(
            await self._append_event(
                tx,
                operation_id=operation_id,
                suffix="work-claimed",
                aggregate_type="work_item",
                aggregate_id=work_id,
                event_type="work.claimed",
                principal=principal,
                payload={"state": updated, "lease_id": lease_id},
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
        )
        return {
            "work_item": updated,
            "lease": lease_view,
            "_event_ids": event_ids,
            "_prior_revision": expected_revision,
            "_new_revision": expected_revision + 1,
            "_resource_type": "work_item",
            "_resource_id": work_id,
        }

    async def _mutate_work_heartbeat(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        work_id = _required_str(payload, "work_item_id")
        self._require(
            principal,
            Capability.WORK_HEARTBEAT,
            ResourceScope.resource(company_id, ResourceType.WORK_ITEM, work_id),
        )
        record, _ = await self._projection(tx, "work_item", work_id)
        state = dict(record["state"])
        if state.get("status") != WorkStatus.CLAIMED.value:
            raise LeaseError("work item is not claimed")
        if state.get("revision") != expected_revision:
            raise ConflictError("stale work item revision")
        cell_id = state.get("cell_id")
        await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.WORK_HEARTBEAT.value},
            cell_id=str(cell_id) if cell_id else None,
            require_bound_cell_target=True,
        )
        lease = await tx.get_lease(work_id, include_inactive=True, now=self.clock())
        if lease is None:
            raise LeaseError("work item has no lease")
        self._assert_lease_owner(lease, principal)
        seconds = _lease_seconds(payload.get("lease_seconds", 300))
        renewed = await tx.renew_lease(
            work_item_id=work_id,
            token=_required_str(payload, "lease_token"),
            ttl=seconds,
            expected_revision=lease["revision"],
            now=self.clock(),
        )
        updated = self._revised_state(state, expected_revision)
        await self._put_state(
            tx,
            "work_item",
            work_id,
            updated,
            expected_projection_revision=record["revision"],
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="work-heartbeat",
            aggregate_type="work_item",
            aggregate_id=work_id,
            event_type="work.heartbeat",
            principal=principal,
            payload={"state": updated, "lease_revision": renewed["revision"]},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "work_item": updated,
            "lease": {**renewed, "principal_id": renewed["agent_id"]},
            "_event_ids": [event_id],
            "_prior_revision": expected_revision,
            "_new_revision": expected_revision + 1,
            "_resource_type": "work_item",
            "_resource_id": work_id,
        }

    async def _mutate_work_complete(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        work_id = _required_str(payload, "work_item_id")
        self._require(
            principal,
            Capability.WORK_COMPLETE,
            ResourceScope.resource(company_id, ResourceType.WORK_ITEM, work_id),
        )
        record, _ = await self._projection(tx, "work_item", work_id)
        state = dict(record["state"])
        if state.get("status") != WorkStatus.CLAIMED.value:
            raise LeaseError("work item is not claimed")
        if state.get("revision") != expected_revision:
            raise ConflictError("stale work item revision")
        cell_id = state.get("cell_id")
        await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.WORK_COMPLETE.value},
            cell_id=str(cell_id) if cell_id else None,
            require_bound_cell_target=True,
        )
        if cell_id is not None:
            cell_record, _ = await self._projection(tx, "cell", cell_id)
            cell_status = cell_record["state"].get("status", cell_record["state"].get("state"))
            if cell_status != CellStatus.ACTIVE.value:
                raise ValidationError("work completion is blocked by cell stop state")
        lease = await tx.get_lease(work_id, include_inactive=True, now=self.clock())
        if lease is None:
            raise LeaseError("work item has no lease")
        self._assert_lease_owner(lease, principal)
        token = _required_str(payload, "lease_token")
        evidence_id = _required_str(payload, "result_evidence_id")
        actual_cost = payload.get("actual_cost")
        if not isinstance(actual_cost, Mapping):
            raise ValidationError("actual_cost is required")
        _require_matching_unit(
            actual_cost,
            state["budget"],
            "actual cost unit differs from work budget",
        )
        actual_minor = _minor_amount(actual_cost.get("amount_minor"), "actual_cost.amount_minor")
        if actual_minor > int(state["budget"]["maximum_cost_minor"]):
            raise ValidationError("actual cost exceeds work budget")
        released = await tx.release_lease(
            work_item_id=work_id,
            token=token,
            expected_revision=lease["revision"],
            now=self.clock(),
        )
        evidence_record, _ = await self._projection(tx, "evidence", evidence_id)
        evidence_state = evidence_record["state"]
        if evidence_state.get("cell_id") != state.get("cell_id"):
            raise ValidationError("result evidence belongs to another cell")
        if evidence_state.get("experiment_id") != state.get("experiment_id"):
            raise ValidationError("result evidence belongs to another experiment")
        updated = self._revised_state(state, expected_revision, status=WorkStatus.COMPLETED.value)
        await self._put_state(
            tx,
            "work_item",
            work_id,
            updated,
            expected_projection_revision=record["revision"],
        )
        runtime_record, _ = await self._projection(tx, "work_runtime", work_id)
        runtime = dict(runtime_record["state"])
        runtime.update(
            lease_id=None,
            result_evidence_ids=[evidence_id],
            actual_cost_minor=actual_minor,
        )
        ledger_spend_minor = await self._apply_work_ledger_spend(
            tx,
            work_state=state,
            runtime=runtime,
            spend_minor=actual_minor,
        )
        await self._put_state(
            tx,
            "work_runtime",
            work_id,
            runtime,
            expected_projection_revision=runtime_record["revision"],
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="work-completed",
            aggregate_type="work_item",
            aggregate_id=work_id,
            event_type="work.completed",
            principal=principal,
            payload={
                "state": updated,
                "result_evidence_id": evidence_id,
                "actual_cost_minor": actual_minor,
                "ledger_spend_minor": ledger_spend_minor,
            },
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "work_item": updated,
            "lease": {**released, "principal_id": released["agent_id"]},
            "result_evidence_id": evidence_id,
            "_event_ids": [event_id],
            "_prior_revision": expected_revision,
            "_new_revision": expected_revision + 1,
            "_resource_type": "work_item",
            "_resource_id": work_id,
        }

    async def _mutate_work_fail(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        return await self._finish_work_without_result(
            tx,
            principal,
            payload,
            expected_revision,
            operation_id,
            correlation_id,
            idempotency_key,
            failed=True,
        )

    async def _mutate_work_release(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        return await self._finish_work_without_result(
            tx,
            principal,
            payload,
            expected_revision,
            operation_id,
            correlation_id,
            idempotency_key,
            failed=False,
        )

    async def _mutate_work_cancel(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        work_id = _required_str(payload, "work_item_id")
        cell_id = _required_str(payload, "cell_id")
        reason = _required_str(payload, "reason")
        if len(reason) > 2_000:
            raise ValidationError("reason is too long")

        allowed = {"work_item_id", "cell_id", "reason", "superseded_by"}
        unknown = set(payload) - allowed
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValidationError(f"work.cancel payload contains unsupported fields: {names}")

        self._require(
            principal,
            Capability.WORK_CANCEL,
            ResourceScope.resource(company_id, ResourceType.CELL, cell_id),
        )
        await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.WORK_CANCEL.value},
            cell_id=cell_id,
            require_bound_cell_target=True,
        )

        record, _ = await self._projection(tx, "work_item", work_id)
        state = dict(record["state"])
        work_cell_id = state.get("cell_id")
        if work_cell_id is None or str(work_cell_id) != cell_id:
            raise ValidationError("cell_id does not match work item")
        if state.get("revision") != expected_revision:
            raise ConflictError("stale work item revision")

        status = state.get("status")
        if status != WorkStatus.OPEN.value:
            raise ValidationError(f"status is {status}")

        now = self.clock()
        lease = await tx.get_lease(work_id, include_inactive=True, now=now)
        if lease is not None and lease.get("status") == "active":
            raise ValidationError("lease_id is active")

        superseded_by: str | None = None
        if payload.get("superseded_by") is not None:
            superseded_by = str(_uuid(payload.get("superseded_by"), "superseded_by"))

        history_entry = {
            "cancelled_at": _jsonable(now),
            "cancelled_by_principal_id": principal.principal_id,
            "reason": reason,
        }
        if superseded_by is not None:
            history_entry["superseded_by"] = superseded_by
        history = list(state.get("cancellation_history") or ())
        history.append(history_entry)
        updated = self._revised_state(
            state,
            expected_revision,
            status=WorkStatus.CANCELLED.value,
            cancellation_history=history,
        )
        await self._put_state(
            tx,
            "work_item",
            work_id,
            updated,
            expected_projection_revision=record["revision"],
        )
        runtime_record, _ = await self._projection(tx, "work_runtime", work_id)
        runtime = dict(runtime_record["state"])
        runtime["lease_id"] = None
        await self._put_state(
            tx,
            "work_runtime",
            work_id,
            runtime,
            expected_projection_revision=runtime_record["revision"],
        )
        decision_id = uuid5(operation_id, "decision")
        decision = {
            "decision_id": str(decision_id),
            "kind": "work.cancel",
            "cell_id": cell_id,
            "work_item_id": work_id,
            "reason": reason,
            "cancelled_by_principal_id": principal.principal_id,
            "cancelled_at": _jsonable(now),
            "work_revision": expected_revision + 1,
        }
        if superseded_by is not None:
            decision["superseded_by"] = superseded_by
        await self._put_state(
            tx,
            "decision",
            decision_id,
            decision,
            expected_projection_revision=0,
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="work-cancelled",
            aggregate_type="work_item",
            aggregate_id=work_id,
            event_type="work.cancelled",
            principal=principal,
            payload={
                "state": updated,
                "reason": reason,
                "superseded_by": superseded_by,
            },
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        decision_event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="work-cancel-decision",
            aggregate_type="decision",
            aggregate_id=decision_id,
            event_type="decision.recorded",
            principal=principal,
            payload={"state": decision},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        audit_details = {
            "reason": reason,
            "work_item_id": work_id,
            "cell_id": cell_id,
            "decision_id": str(decision_id),
        }
        if superseded_by is not None:
            audit_details["superseded_by"] = superseded_by
        return {
            "work_item": updated,
            "decision": decision,
            "cancellation": history_entry,
            "_event_ids": [event_id, decision_event_id],
            "_prior_revision": expected_revision,
            "_new_revision": expected_revision + 1,
            "_resource_type": "work_item",
            "_resource_id": work_id,
            "_audit_details": audit_details,
        }

    async def _finish_work_without_result(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
        *,
        failed: bool,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        work_id = _required_str(payload, "work_item_id")
        capability = Capability.WORK_FAIL if failed else Capability.WORK_RELEASE
        self._require(
            principal,
            capability,
            ResourceScope.resource(company_id, ResourceType.WORK_ITEM, work_id),
        )
        record, _ = await self._projection(tx, "work_item", work_id)
        state = dict(record["state"])
        await self._assert_agent_capabilities(
            tx,
            principal,
            {capability.value},
            cell_id=str(state.get("cell_id")) if state.get("cell_id") else None,
            require_bound_cell_target=True,
        )
        if state.get("status") != WorkStatus.CLAIMED.value:
            raise LeaseError("work item is not claimed")
        if state.get("revision") != expected_revision:
            raise ConflictError("stale work item revision")
        lease = await tx.get_lease(work_id, include_inactive=True, now=self.clock())
        if lease is None:
            raise LeaseError("work item has no lease")
        self._assert_lease_owner(lease, principal)
        released = await tx.release_lease(
            work_item_id=work_id,
            token=_required_str(payload, "lease_token"),
            expected_revision=lease["revision"],
            now=self.clock(),
        )
        runtime_record, _ = await self._projection(tx, "work_runtime", work_id)
        runtime = dict(runtime_record["state"])
        exhausted = int(runtime.get("attempt", 0)) >= int(state["max_attempts"])
        retryable = bool(payload.get("retryable", False))
        status = (
            WorkStatus.OPEN.value
            if (not failed or retryable) and not exhausted
            else WorkStatus.FAILED.value
        )
        failure_code = _required_str(payload, "failure_code") if failed else None
        updated = self._revised_state(state, expected_revision, status=status)
        await self._put_state(
            tx,
            "work_item",
            work_id,
            updated,
            expected_projection_revision=record["revision"],
        )
        runtime.update(lease_id=None, failure_code=failure_code)
        ledger_spend_minor = 0
        if failed and status == WorkStatus.FAILED.value:
            actual_cost = payload.get("actual_cost")
            if actual_cost is None:
                spend_minor = _minor_amount(
                    state["budget"]["maximum_cost_minor"], "work.budget.maximum_cost_minor"
                )
            else:
                if not isinstance(actual_cost, Mapping):
                    raise ValidationError("actual_cost must be an object")
                _require_matching_unit(
                    actual_cost,
                    state["budget"],
                    "actual cost unit differs from work budget",
                )
                spend_minor = _minor_amount(
                    actual_cost.get("amount_minor"), "actual_cost.amount_minor"
                )
                if spend_minor > int(state["budget"]["maximum_cost_minor"]):
                    raise ValidationError("actual cost exceeds work budget")
            ledger_spend_minor = await self._apply_work_ledger_spend(
                tx,
                work_state=state,
                runtime=runtime,
                spend_minor=spend_minor,
            )
        await self._put_state(
            tx,
            "work_runtime",
            work_id,
            runtime,
            expected_projection_revision=runtime_record["revision"],
        )
        event_type = "work.failed" if failed else "work.released"
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix=event_type,
            aggregate_type="work_item",
            aggregate_id=work_id,
            event_type=event_type,
            principal=principal,
            payload={
                "state": updated,
                "failure_code": failure_code,
                "ledger_spend_minor": ledger_spend_minor,
            },
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "work_item": updated,
            "lease": {**released, "principal_id": released["agent_id"]},
            "_event_ids": [event_id],
            "_prior_revision": expected_revision,
            "_new_revision": expected_revision + 1,
            "_resource_type": "work_item",
            "_resource_id": work_id,
        }

    async def _mutate_experiment_register(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        cell_id = _required_str(payload, "cell_id")
        self._require(
            principal,
            Capability.EXPERIMENT_REGISTER,
            ResourceScope.resource(company_id, ResourceType.CELL, cell_id),
        )
        await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.EXPERIMENT_REGISTER.value},
            cell_id=cell_id,
        )
        if expected_revision != 0:
            raise ConflictError("new experiment registration requires expected_revision 0")
        cell_record, _ = await self._projection(tx, "cell", cell_id)
        cell_status = cell_record["state"].get("status", cell_record["state"].get("state"))
        if cell_status != CellStatus.ACTIVE.value:
            raise ValidationError("experiment registration requires an active cell")
        offer_version_id = _required_str(payload, "offer_version_id")
        offer_record, _ = await self._projection(tx, "offer", offer_version_id)
        if offer_record["state"].get("cell_id") != cell_id:
            raise ValidationError("offer version belongs to another cell")
        executors = {
            _uuid(item, "executor_principal_id")
            for item in payload.get("executor_principal_ids", ())
        }
        evaluator = _uuid(payload.get("evaluator_principal_id"), "evaluator_principal_id")
        if evaluator in executors:
            raise SeparationOfDutiesError(
                "experiment evaluator must be distinct from every executor"
            )
        at = self.clock()
        values = dict(payload)
        values.update(
            experiment_id=_uuid(
                payload.get("experiment_id", uuid5(operation_id, "experiment")),
                "experiment_id",
            ),
            company_id=_uuid(company_id, "company_id"),
            revision=1,
            state=ExperimentStatus.REGISTERED,
            executor_principal_ids=executors,
            evaluator_principal_id=evaluator,
            registered_by_principal_id=_principal_uuid(principal),
            registered_at=at,
        )
        experiment = Experiment.model_validate(values)
        company_record, _ = await self._projection(tx, "company", company_id)
        constitution_record, _ = await self._projection(
            tx,
            "constitution",
            company_record["state"]["active_constitution_id"],
        )
        _require_matching_unit(
            experiment.budget.model_dump(mode="json"),
            constitution_record["state"]["budget_policy"],
            "experiment budget unit must match the constitution budget policy",
        )
        await self._put_model(
            tx,
            "experiment",
            experiment.experiment_id,
            experiment,
            expected_projection_revision=0,
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="experiment-registered",
            aggregate_type="experiment",
            aggregate_id=experiment.experiment_id,
            event_type="experiment.registered",
            principal=principal,
            payload={"state": experiment},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "experiment": experiment,
            "_event_ids": [event_id],
            "_prior_revision": 0,
            "_new_revision": 1,
            "_resource_type": "experiment",
            "_resource_id": experiment.experiment_id,
        }

    async def _mutate_experiment_start(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        experiment_id = _required_str(payload, "experiment_id")
        self._require(
            principal,
            Capability.EXPERIMENT_START,
            ResourceScope.resource(company_id, ResourceType.EXPERIMENT, experiment_id),
        )
        record, _ = await self._projection(tx, "experiment", experiment_id)
        state = dict(record["state"])
        if state.get("revision") != expected_revision:
            raise ConflictError("stale experiment revision")
        if state.get("state") != ExperimentStatus.REGISTERED.value:
            raise ValidationError("experiment is not registered")
        cell_id = str(state["cell_id"])
        await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.EXPERIMENT_START.value},
            cell_id=cell_id,
        )
        cell_record, _ = await self._projection(tx, "cell", cell_id)
        cell_state = cell_record["state"]
        if cell_state.get("status", cell_state.get("state")) != CellStatus.ACTIVE.value:
            raise ValidationError("experiment start requires an active cell")
        at = self.clock()
        window = state["window"]
        starts_at = datetime.fromisoformat(str(window["starts_at"]).replace("Z", "+00:00"))
        ends_at = datetime.fromisoformat(str(window["ends_at"]).replace("Z", "+00:00"))
        if not starts_at <= at < ends_at:
            raise ValidationError("experiment is outside its preregistered window")
        ledgers = await self._constraint_ledgers(
            tx,
            cell_state=cell_state,
            stage=AllocationStage.EXPLORATION.value,
        )
        amount_minor = _minor_amount(
            state["budget"]["amount_minor"], "experiment.budget.amount_minor"
        )
        company_record, _ = await self._projection(tx, "company", company_id)
        constitution_record, _ = await self._projection(
            tx,
            "constitution",
            company_record["state"]["active_constitution_id"],
        )
        budget_policy = constitution_record["state"]["budget_policy"]
        _require_matching_unit(
            state["budget"],
            budget_policy,
            "experiment budget unit must match the constitution budget policy",
        )
        experiment_unit, _experiment_currency = identity_of(state["budget"])
        if amount_minor > int(budget_policy["experiment_limit_minor"]):
            raise BudgetError("experiment budget exceeds the constitution experiment limit")
        reservation_group_id = str(uuid5(_uuid(experiment_id, "experiment_id"), "reservation"))
        reservation = await tx.reserve_budget_group(
            reservation_group_id=reservation_group_id,
            budget_ids={role: budget["budget_id"] for role, budget in ledgers.items()},
            amount=quantity_to_ledger(amount_minor, experiment_unit),
            purpose="preregistered experiment budget",
            expected_revisions={
                budget["budget_id"]: budget["revision"] for budget in ledgers.values()
            },
            expires_at=ends_at,
            idempotency_key=idempotency_key,
            metadata={
                "experiment_id": experiment_id,
                "amount_minor": amount_minor,
                "stage": AllocationStage.EXPLORATION.value,
            },
            now=at,
        )
        updated = self._revised_state(
            state,
            expected_revision,
            state=ExperimentStatus.RUNNING.value,
            started_at=_jsonable(at),
            reservation_id=reservation_group_id,
        )
        await self._put_state(
            tx,
            "experiment",
            experiment_id,
            updated,
            expected_projection_revision=record["revision"],
        )
        await self._put_state(
            tx,
            "experiment_runtime",
            experiment_id,
            {
                "reservation_id": reservation_group_id,
                "reservation_group_id": reservation_group_id,
                "reservation_ids": reservation["reservation_ids"],
                "budget_ids": reservation["budget_ids"],
                "stage": AllocationStage.EXPLORATION.value,
                "actual_cost_minor": 0,
                "stop_state": "clear",
            },
            expected_projection_revision=0,
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="experiment-started",
            aggregate_type="experiment",
            aggregate_id=experiment_id,
            event_type="experiment.started",
            principal=principal,
            payload={
                "state": updated,
                "reservation_group_id": reservation_group_id,
                "reservation_ids": reservation["reservation_ids"],
            },
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "experiment": updated,
            "reservation": reservation,
            "_event_ids": [event_id],
            "_prior_revision": expected_revision,
            "_new_revision": expected_revision + 1,
            "_resource_type": "experiment",
            "_resource_id": experiment_id,
        }

    async def _mutate_experiment_amend(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        experiment_id = _required_str(payload, "experiment_id")
        self._require(
            principal,
            Capability.EXPERIMENT_AMEND,
            ResourceScope.resource(company_id, ResourceType.EXPERIMENT, experiment_id),
        )
        record, _ = await self._projection(tx, "experiment", experiment_id)
        state = dict(record["state"])
        if state.get("revision") != expected_revision:
            raise ConflictError("stale experiment revision")
        current_status = str(state.get("state") or "")
        if current_status != ExperimentStatus.RUNNING.value:
            raise ValidationError(
                f"only a running experiment can be amended (current state: {current_status})"
            )
        cell_id = str(state["cell_id"])
        await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.EXPERIMENT_AMEND.value},
            cell_id=cell_id,
        )
        founder_decision_ref = _required_str(payload, "founder_decision_ref")
        if len(founder_decision_ref) > 500:
            raise ValidationError("founder_decision_ref is too long")

        allowed = {
            "experiment_id",
            "window_ends_at",
            "stop_rules",
            "treatment",
            "hypothesis",
            "founder_decision_ref",
            "budget_amount_minor",
            "max_external_messages",
        }
        frozen = {
            "baseline",
            "target_population",
            "metrics",
            "success_thresholds",
            "failure_thresholds",
            "budget",
            "permitted_actions",
            "executor_principal_ids",
            "evaluator_principal_id",
            "cell_id",
            "offer_version_id",
            "company_id",
            "state",
            "revision",
            "window",
            "success_threshold",
            "failure_threshold",
        }
        unknown = set(payload) - allowed
        if unknown:
            blocked = unknown & frozen
            names = ", ".join(sorted(blocked or unknown))
            if blocked:
                raise ValidationError(
                    "experiment.amend cannot change frozen evaluation criteria "
                    f"or immutable lineage: {names}"
                )
            raise ValidationError(f"experiment.amend payload contains unsupported fields: {names}")

        current_window = dict(state["window"])
        current_end = parse_aware_datetime(current_window["ends_at"], "window.ends_at")
        current_rules = _jsonable(list(state["stop_rules"]))
        current_treatment = str(state["treatment"])
        current_hypothesis = str(state["hypothesis"])
        current_budget = dict(state["budget"])
        current_budget_minor = _minor_amount(
            current_budget["amount_minor"], "experiment.budget.amount_minor"
        )
        current_override = _experiment_message_override(state)
        now = self.clock()
        changed: list[str] = []
        next_window = dict(current_window)
        next_rules = list(current_rules)
        next_treatment = current_treatment
        next_hypothesis = current_hypothesis
        next_budget = dict(current_budget)
        next_budget_minor = current_budget_minor
        next_override = current_override
        reservation_adjusted = None

        if "window_ends_at" in payload:
            new_end = parse_aware_datetime(payload["window_ends_at"], "window_ends_at")
            if new_end < current_end:
                raise ValidationError("window_ends_at must be later than the current window end")
            if new_end > current_end:
                if new_end <= now:
                    raise ValidationError("window_ends_at must be later than now")
                validated_window = ExperimentWindow.model_validate(
                    {"starts_at": current_window["starts_at"], "ends_at": new_end}
                )
                next_window = _jsonable(validated_window)
                changed.append("window")

        if "stop_rules" in payload:
            raw_rules = payload["stop_rules"]
            if not isinstance(raw_rules, list) or not raw_rules:
                raise ValidationError("stop_rules must contain at least one rule")
            if len(raw_rules) > 25:
                raise ValidationError("stop_rules exceeds the maximum of 25")
            parsed_rules = [StopRule.model_validate(item) for item in raw_rules]
            rule_ids = [rule.rule_id for rule in parsed_rules]
            if len(rule_ids) != len(set(rule_ids)):
                raise ValidationError("stop rule IDs must be unique")
            next_rules = _jsonable(parsed_rules)
            if next_rules != current_rules:
                changed.append("stop_rules")

        if "treatment" in payload:
            treatment = payload["treatment"]
            if not isinstance(treatment, str) or not treatment.strip():
                raise ValidationError("treatment is required")
            if len(treatment) > 2_000:
                raise ValidationError("treatment exceeds 2000 characters")
            next_treatment = treatment.strip()
            if next_treatment != current_treatment:
                changed.append("treatment")

        if "hypothesis" in payload:
            hypothesis = payload["hypothesis"]
            if not isinstance(hypothesis, str) or not hypothesis.strip():
                raise ValidationError("hypothesis is required")
            if len(hypothesis) > 2_000:
                raise ValidationError("hypothesis exceeds 2000 characters")
            next_hypothesis = hypothesis.strip()
            if next_hypothesis != current_hypothesis:
                changed.append("hypothesis")

        if "budget_amount_minor" in payload:
            next_budget_minor = _minor_amount(payload["budget_amount_minor"], "budget_amount_minor")
            if next_budget_minor <= 0:
                raise ValidationError("budget_amount_minor must be greater than zero")
            if next_budget_minor != current_budget_minor:
                company_record, _ = await self._projection(tx, "company", company_id)
                constitution_record, _ = await self._projection(
                    tx,
                    "constitution",
                    company_record["state"]["active_constitution_id"],
                )
                budget_policy = constitution_record["state"]["budget_policy"]
                _require_matching_unit(
                    current_budget,
                    budget_policy,
                    "experiment budget unit must match the constitution budget policy",
                )
                if next_budget_minor > int(budget_policy["experiment_limit_minor"]):
                    raise BudgetError("experiment budget exceeds the constitution experiment limit")
                reservation_adjusted = await self._adjust_experiment_reservation_hold(
                    tx,
                    experiment_id=experiment_id,
                    experiment=state,
                    new_amount_minor=next_budget_minor,
                )
                unit, currency = identity_of(current_budget)
                next_budget = dump_budget_amount(
                    unit=unit,
                    amount_minor=next_budget_minor,
                    currency=currency,
                )
                changed.append("budget_amount_minor")

        if "max_external_messages" in payload:
            next_override = _message_cap_value(
                payload["max_external_messages"], "max_external_messages"
            )
            if next_override != current_override:
                changed.append("max_external_messages")

        if next_override is not None and next_override > next_budget_minor:
            raise ValidationError(
                "max_external_messages cannot exceed the experiment try budget "
                f"({next_budget_minor})"
            )
        runtime_record = await tx.get_projection("experiment_runtime", experiment_id)
        recorded_send_count = 0
        if runtime_record is not None:
            recorded_send_count = len(runtime_record["state"].get("recorded_send_ids") or ())
        if next_override is not None and next_override < recorded_send_count:
            raise ValidationError(
                f"max_external_messages cannot drop below recorded sends ({recorded_send_count})"
            )
        if (
            current_override is not None
            and "max_external_messages" not in payload
            and current_override > next_budget_minor
        ):
            raise ValidationError(
                "amended budget is below the existing max_external_messages override "
                f"({current_override}); supply a lower max_external_messages"
            )

        if not changed:
            raise ValidationError(
                "experiment.amend requires a change to window_ends_at, stop_rules, "
                "treatment, hypothesis, budget_amount_minor, or max_external_messages"
            )

        prior_snapshot = {
            "window": _jsonable(current_window),
            "stop_rules": current_rules,
            "treatment": current_treatment,
            "hypothesis": current_hypothesis,
            "budget": _jsonable(current_budget),
            "max_external_messages": current_override,
        }
        new_snapshot = {
            "window": next_window,
            "stop_rules": next_rules,
            "treatment": next_treatment,
            "hypothesis": next_hypothesis,
            "budget": _jsonable(next_budget),
            "max_external_messages": next_override,
        }
        amendment = {
            "amended_at": _jsonable(now),
            "amended_by_principal_id": principal.principal_id,
            "founder_decision_ref": founder_decision_ref,
            "prior": prior_snapshot,
            "new": new_snapshot,
            "changed_fields": changed,
        }
        history = list(state.get("amendment_history") or ())
        history.append(amendment)
        revisions: dict[str, Any] = {
            "window": next_window,
            "stop_rules": next_rules,
            "treatment": next_treatment,
            "hypothesis": next_hypothesis,
            "budget": next_budget,
            "amendment_history": history,
            "max_external_messages": next_override,
        }
        updated = self._revised_state(
            state,
            expected_revision,
            **revisions,
        )
        await self._put_state(
            tx,
            "experiment",
            experiment_id,
            updated,
            expected_projection_revision=record["revision"],
        )
        decision_id = uuid5(operation_id, "decision")
        decision = {
            "decision_id": str(decision_id),
            "kind": "experiment.amend",
            "experiment_id": experiment_id,
            "cell_id": cell_id,
            "founder_decision_ref": founder_decision_ref,
            "prior": prior_snapshot,
            "new": new_snapshot,
            "changed_fields": changed,
            "amended_by_principal_id": principal.principal_id,
            "amended_at": _jsonable(now),
            "experiment_revision": expected_revision + 1,
            "reservation_amount_minor": (
                next_budget_minor if reservation_adjusted is not None else None
            ),
        }
        await self._put_state(
            tx,
            "decision",
            decision_id,
            decision,
            expected_projection_revision=0,
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="experiment-amended",
            aggregate_type="experiment",
            aggregate_id=experiment_id,
            event_type="experiment.amended",
            principal=principal,
            payload={
                "state": updated,
                "founder_decision_ref": founder_decision_ref,
                "prior": prior_snapshot,
                "new": new_snapshot,
                "changed_fields": changed,
            },
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        decision_event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="experiment-amend-decision",
            aggregate_type="decision",
            aggregate_id=decision_id,
            event_type="decision.recorded",
            principal=principal,
            payload={"state": decision},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "experiment": updated,
            "decision": decision,
            "amendment": amendment,
            "_event_ids": [event_id, decision_event_id],
            "_prior_revision": expected_revision,
            "_new_revision": expected_revision + 1,
            "_resource_type": "experiment",
            "_resource_id": experiment_id,
            "_audit_details": {
                "founder_decision_ref": founder_decision_ref,
                "changed_fields": changed,
                "prior": prior_snapshot,
                "new": new_snapshot,
                "decision_id": str(decision_id),
                "reservation_amount_minor": (
                    next_budget_minor if reservation_adjusted is not None else None
                ),
            },
        }

    async def _mutate_experiment_record_send(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        experiment_id = _required_str(payload, "experiment_id")
        send_id = str(_uuid(payload.get("send_id"), "send_id"))
        self._require(
            principal,
            Capability.EXPERIMENT_RECORD_SEND,
            ResourceScope.resource(company_id, ResourceType.EXPERIMENT, experiment_id),
        )
        if expected_revision != 0:
            raise ConflictError("new recorded send requires expected_revision 0")
        allowed = {
            "experiment_id",
            "send_id",
            "target",
            "channel",
            "sent_at",
            "founder_signoff_ref",
            "evidence_id",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError(
                "experiment.record_send payload contains unsupported fields: "
                + ", ".join(sorted(unknown))
            )
        experiment_record, _ = await self._projection(tx, "experiment", experiment_id)
        experiment = dict(experiment_record["state"])
        current_status = str(experiment.get("state") or "")
        if current_status != ExperimentStatus.RUNNING.value:
            raise ValidationError(
                f"only a running experiment can record a send (current state: {current_status})"
            )
        cell_id = str(experiment["cell_id"])
        await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.EXPERIMENT_RECORD_SEND.value},
            cell_id=cell_id,
        )
        cell_record, _ = await self._projection(tx, "cell", cell_id)
        cell_status = cell_record["state"].get("status", cell_record["state"].get("state"))
        if cell_status != CellStatus.ACTIVE.value:
            raise ValidationError("recording a send is blocked by cell stop state")
        founder_signoff_ref = _required_str(payload, "founder_signoff_ref")
        if len(founder_signoff_ref) > 500:
            raise ValidationError("founder_signoff_ref is too long")
        target = _required_str(payload, "target")
        if len(target) > 200:
            raise ValidationError("target is too long")
        channel = _required_str(payload, "channel")
        if len(channel) > 64:
            raise ValidationError("channel is too long")
        sent_at = parse_aware_datetime(payload.get("sent_at"), "sent_at")
        now = self.clock()
        if sent_at > now:
            raise ValidationError("sent_at must not be in the future")
        # sent_at is the founder/Gmail-verified send time. Evidence observed_at
        # is a separate fact and is not cross-checked against it.
        sent_at_text = _jsonable(sent_at)
        evidence_id = _required_str(payload, "evidence_id")
        evidence_record, _ = await self._projection(tx, "evidence", evidence_id)
        evidence_state = evidence_record["state"]
        if str(evidence_state.get("cell_id") or "") != cell_id:
            raise ValidationError("send evidence belongs to another cell")
        if str(evidence_state.get("experiment_id") or "") != experiment_id:
            raise ValidationError("send evidence belongs to another experiment")
        fingerprint = _send_fingerprint(
            experiment_id=experiment_id,
            target=target,
            channel=channel,
            sent_at=sent_at_text,
        )
        company_record, _ = await self._projection(tx, "company", company_id)
        constitution_record, _ = await self._projection(
            tx,
            "constitution",
            company_record["state"]["active_constitution_id"],
        )
        message_cap = _effective_message_cap(experiment, constitution_record["state"])
        existing_send = await tx.get_projection("experiment_send", send_id)
        if existing_send is not None:
            existing_state = dict(existing_send["state"])
            same = (
                existing_state.get("experiment_id") == experiment_id
                and existing_state.get("target") == target
                and existing_state.get("channel") == channel
                and existing_state.get("sent_at") == sent_at_text
                and existing_state.get("founder_signoff_ref") == founder_signoff_ref
                and existing_state.get("evidence_id") == evidence_id
            )
            if not same:
                raise ConflictError("send_id already records a different external send")
            decision_id = str(existing_state["decision_id"])
            decision_record, _ = await self._projection(tx, "decision", decision_id)
            runtime_record, _ = await self._projection(tx, "experiment_runtime", experiment_id)
            remaining = _external_messages_remaining(
                cap=message_cap,
                recorded_send_count=len(runtime_record["state"].get("recorded_send_ids") or ()),
            )
            event_id = await self._append_event(
                tx,
                operation_id=operation_id,
                suffix="experiment-send-already-recorded",
                aggregate_type="experiment_send",
                aggregate_id=send_id,
                event_type="experiment.send_already_recorded",
                principal=principal,
                payload={"state": existing_state, "already_recorded": True, "spend_minor": 0},
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
            return {
                "send": existing_state,
                "decision": decision_record["state"],
                "already_recorded": True,
                "ledger_spend_minor": 0,
                "max_external_messages_per_experiment": message_cap,
                "external_messages_remaining": remaining,
                "_event_ids": [event_id],
                "_prior_revision": 0,
                "_new_revision": 1,
                "_resource_type": "experiment_send",
                "_resource_id": send_id,
                "_audit_details": {
                    "send_id": send_id,
                    "experiment_id": experiment_id,
                    "cell_id": cell_id,
                    "founder_signoff_ref": founder_signoff_ref,
                    "evidence_id": evidence_id,
                    "already_recorded": True,
                    "decision_id": decision_id,
                    "spend_minor": 0,
                    "max_external_messages_per_experiment": message_cap,
                    "external_messages_remaining": remaining,
                },
            }
        runtime_record, _ = await self._projection(tx, "experiment_runtime", experiment_id)
        experiment_runtime = dict(runtime_record["state"])
        fingerprints = dict(experiment_runtime.get("send_fingerprints") or {})
        recorded_ids = list(experiment_runtime.get("recorded_send_ids") or ())
        prior_send_id = fingerprints.get(fingerprint)
        if prior_send_id and str(prior_send_id) != send_id:
            raise ValidationError("external send already recorded")
        remaining_before = _external_messages_remaining(
            cap=message_cap,
            recorded_send_count=len(recorded_ids),
        )
        if remaining_before <= 0:
            raise ValidationError(
                f"external message cap reached ({remaining_before} of {message_cap} remaining)"
            )
        fingerprints[fingerprint] = send_id
        if send_id not in recorded_ids:
            recorded_ids.append(send_id)
        remaining_after = _external_messages_remaining(
            cap=message_cap,
            recorded_send_count=len(recorded_ids),
        )
        await self._apply_experiment_try_spend(
            tx,
            experiment_id=experiment_id,
            spend_minor=1,
            extra_runtime={
                "send_fingerprints": fingerprints,
                "recorded_send_ids": recorded_ids,
            },
        )
        decision_id = str(uuid5(operation_id, "decision"))
        send_state = {
            "schema_version": 1,
            "send_id": send_id,
            "experiment_id": experiment_id,
            "cell_id": cell_id,
            "company_id": company_id,
            "target": target,
            "channel": channel,
            "sent_at": sent_at_text,
            "founder_signoff_ref": founder_signoff_ref,
            "evidence_id": evidence_id,
            "fingerprint": fingerprint,
            "spend_minor": 1,
            "recorded_at": _jsonable(now),
            "recorded_by_principal_id": principal.principal_id,
            "decision_id": decision_id,
            "revision": 1,
        }
        await self._put_state(
            tx,
            "experiment_send",
            send_id,
            send_state,
            expected_projection_revision=0,
        )
        decision = {
            "decision_id": decision_id,
            "kind": "experiment.record_send",
            "experiment_id": experiment_id,
            "cell_id": cell_id,
            "send_id": send_id,
            "target": target,
            "channel": channel,
            "sent_at": sent_at_text,
            "founder_signoff_ref": founder_signoff_ref,
            "evidence_id": evidence_id,
            "spend_minor": 1,
            "max_external_messages_per_experiment": message_cap,
            "external_messages_remaining": remaining_after,
            "recorded_by_principal_id": principal.principal_id,
            "recorded_at": _jsonable(now),
        }
        await self._put_state(
            tx,
            "decision",
            decision_id,
            decision,
            expected_projection_revision=0,
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="experiment-send-recorded",
            aggregate_type="experiment_send",
            aggregate_id=send_id,
            event_type="experiment.send_recorded",
            principal=principal,
            payload={"state": send_state, "spend_minor": 1},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        decision_event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="experiment-send-decision",
            aggregate_type="decision",
            aggregate_id=decision_id,
            event_type="decision.recorded",
            principal=principal,
            payload={"state": decision},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "send": send_state,
            "decision": decision,
            "already_recorded": False,
            "ledger_spend_minor": 1,
            "max_external_messages_per_experiment": message_cap,
            "external_messages_remaining": remaining_after,
            "_event_ids": [event_id, decision_event_id],
            "_prior_revision": 0,
            "_new_revision": 1,
            "_resource_type": "experiment_send",
            "_resource_id": send_id,
            "_audit_details": {
                "send_id": send_id,
                "experiment_id": experiment_id,
                "cell_id": cell_id,
                "target": target,
                "channel": channel,
                "sent_at": sent_at_text,
                "founder_signoff_ref": founder_signoff_ref,
                "evidence_id": evidence_id,
                "decision_id": decision_id,
                "spend_minor": 1,
                "max_external_messages_per_experiment": message_cap,
                "external_messages_remaining": remaining_after,
            },
        }

    async def _mutate_experiment_close(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        experiment_id = _required_str(payload, "experiment_id")
        self._require(
            principal,
            Capability.EXPERIMENT_CLOSE,
            ResourceScope.resource(company_id, ResourceType.EXPERIMENT, experiment_id),
        )
        record, _ = await self._projection(tx, "experiment", experiment_id)
        state = dict(record["state"])
        if state.get("revision") != expected_revision:
            raise ConflictError("stale experiment revision")
        if state.get("state") != ExperimentStatus.RUNNING.value:
            raise ValidationError("only a running experiment can close")
        await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.EXPERIMENT_CLOSE.value},
            cell_id=str(state["cell_id"]),
        )
        actual = payload.get("actual_cost")
        if not isinstance(actual, Mapping):
            raise ValidationError("actual_cost is required")
        _require_matching_unit(
            actual,
            state["budget"],
            "actual cost unit differs from experiment budget",
        )
        actual_minor = _minor_amount(actual.get("amount_minor"), "actual_cost.amount_minor")
        if actual_minor > int(state["budget"]["amount_minor"]):
            raise ValidationError("actual cost exceeds preregistered experiment budget")
        reason = _optional_str(payload, "reason", max_length=2_000)
        founder_decision_ref = _optional_str(payload, "founder_decision_ref", max_length=500)
        updated = await self._close_running_experiment(
            tx,
            principal,
            experiment_id=experiment_id,
            record=record,
            actual_minor=actual_minor,
            operation_id=operation_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            reason=reason,
            founder_decision_ref=founder_decision_ref,
        )
        runtime_record, _ = await self._projection(tx, "experiment_runtime", experiment_id)
        reservation_group_id = str(
            runtime_record["state"].get(
                "reservation_group_id", runtime_record["state"]["reservation_id"]
            )
        )
        committed = await tx.get_reservation_group(reservation_group_id)
        event_id = str(uuid5(operation_id, "experiment-closed"))
        audit_details: dict[str, Any] = {}
        if reason is not None:
            audit_details["reason"] = reason
        if founder_decision_ref is not None:
            audit_details["founder_decision_ref"] = founder_decision_ref
        result: dict[str, Any] = {
            "experiment": updated,
            "reservation": committed,
            "_event_ids": [event_id],
            "_prior_revision": expected_revision,
            "_new_revision": expected_revision + 1,
            "_resource_type": "experiment",
            "_resource_id": experiment_id,
        }
        if audit_details:
            result["_audit_details"] = audit_details
        return result

    async def _mutate_evidence_append(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        return await self._append_evidence_record(
            tx,
            principal,
            payload,
            expected_revision,
            operation_id,
            correlation_id,
            idempotency_key,
            capability=Capability.EVIDENCE_APPEND,
        )

    async def _mutate_economics_append(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        return await self._append_evidence_record(
            tx,
            principal,
            payload,
            expected_revision,
            operation_id,
            correlation_id,
            idempotency_key,
            capability=Capability.ECONOMICS_APPEND_ATTESTED,
        )

    async def _append_evidence_record(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
        *,
        capability: Capability,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        cell_id = _required_str(payload, "cell_id")
        self._require(
            principal,
            capability,
            ResourceScope.resource(company_id, ResourceType.CELL, cell_id),
        )
        await self._assert_agent_capabilities(tx, principal, {capability.value}, cell_id=cell_id)
        if expected_revision != 0:
            raise ConflictError("new evidence requires expected_revision 0")
        experiment_id = payload.get("experiment_id")
        experiment_record: dict[str, Any] | None = None
        if experiment_id is not None:
            experiment_record, _ = await self._projection(tx, "experiment", experiment_id)
            if experiment_record["state"].get("cell_id") != cell_id:
                raise ValidationError("experiment belongs to another cell")
            work_records = await tx.list_projections(projection_type="work_item", limit=500)
            has_live_lease = False
            for work in work_records:
                work_state = work["state"]
                if (
                    work_state.get("experiment_id") != str(experiment_id)
                    or work_state.get("status") != WorkStatus.CLAIMED.value
                ):
                    continue
                lease = await tx.get_lease(
                    work_state["work_item_id"],
                    include_inactive=False,
                    now=self.clock(),
                )
                if lease is None:
                    continue
                try:
                    self._assert_lease_owner(lease, principal)
                except LeaseError:
                    continue
                has_live_lease = True
                break
            lineage = payload.get("lineage")
            correction = isinstance(lineage, Mapping) and lineage.get("relation") != "original"
            if not has_live_lease and not correction:
                raise LeaseError("evidence append requires a live execution lease")
        lineage = payload.get("lineage")
        if isinstance(lineage, Mapping) and lineage.get("related_evidence_id") is not None:
            related, _ = await self._projection(tx, "evidence", lineage["related_evidence_id"])
            if related["state"].get("cell_id") != cell_id:
                raise ValidationError("evidence lineage cannot cross cells")
        at = self.clock()
        values = dict(payload)
        values.update(
            evidence_id=_uuid(
                payload.get("evidence_id", uuid5(operation_id, "evidence")),
                "evidence_id",
            ),
            company_id=_uuid(company_id, "company_id"),
            cell_id=_uuid(cell_id, "cell_id"),
            recorded_at=at,
            recorded_by_principal_id=_principal_uuid(principal),
            agent_run_id=self._principal_run_uuid(principal),
        )
        evidence = EvidenceEnvelope.model_validate(values)
        await self._put_model(
            tx,
            "evidence",
            evidence.evidence_id,
            evidence,
            expected_projection_revision=0,
        )
        event_ids = [
            await self._append_event(
                tx,
                operation_id=operation_id,
                suffix="evidence-appended",
                aggregate_type="evidence",
                aggregate_id=evidence.evidence_id,
                event_type="evidence.appended",
                principal=principal,
                payload={"state": evidence, "content_digest": evidence.content_digest},
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
            )
        ]
        result: dict[str, Any] = {"evidence": evidence}
        if evidence.type.value in {"policy_violation", "security_incident"}:
            cell_record, _ = await self._projection(tx, "cell", cell_id)
            cell_state = dict(cell_record["state"])
            if cell_state.get("status", cell_state.get("state")) in {
                CellStatus.ACTIVE.value,
                CellStatus.READY.value,
            }:
                cell_revision = int(cell_state["revision"])
                stopped_cell = self._revised_state(
                    cell_state,
                    cell_revision,
                    state=CellStatus.PAUSED.value,
                    status=CellStatus.PAUSED.value,
                    stop_reason=evidence.type.value,
                    updated_at=_jsonable(at),
                )
                await self._put_state(
                    tx,
                    "cell",
                    cell_id,
                    stopped_cell,
                    expected_projection_revision=cell_record["revision"],
                )
                event_ids.append(
                    await self._append_event(
                        tx,
                        operation_id=operation_id,
                        suffix="cell-auto-paused",
                        aggregate_type="cell",
                        aggregate_id=cell_id,
                        event_type="cell.auto_paused",
                        principal=principal,
                        payload={
                            "state": stopped_cell,
                            "trigger_evidence_id": str(evidence.evidence_id),
                        },
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key,
                    )
                )
                result["cell"] = stopped_cell
            if (
                experiment_record is not None
                and experiment_record["state"].get("state") == ExperimentStatus.RUNNING.value
            ):
                experiment_state = dict(experiment_record["state"])
                experiment_revision = int(experiment_state["revision"])
                stopped_experiment = self._revised_state(
                    experiment_state,
                    experiment_revision,
                    state=ExperimentStatus.CANCELLED.value,
                    stopped_at=_jsonable(at),
                    stop_reason=evidence.type.value,
                )
                await self._put_state(
                    tx,
                    "experiment",
                    experiment_id,
                    stopped_experiment,
                    expected_projection_revision=experiment_record["revision"],
                )
                runtime = await tx.get_projection("experiment_runtime", str(experiment_id))
                if runtime is not None:
                    reservation_group_id = str(
                        runtime["state"].get(
                            "reservation_group_id",
                            runtime["state"]["reservation_id"],
                        )
                    )
                    reservation = await tx.get_reservation_group(reservation_group_id)
                    if reservation is not None and reservation["status"] == "active":
                        await tx.release_reservation_group(
                            reservation_group_id=reservation_group_id,
                            expected_revisions={
                                member["reservation_id"]: member["revision"]
                                for member in reservation["members"]
                            },
                            now=at,
                        )
                event_ids.append(
                    await self._append_event(
                        tx,
                        operation_id=operation_id,
                        suffix="experiment-auto-cancelled",
                        aggregate_type="experiment",
                        aggregate_id=experiment_id,
                        event_type="experiment.auto_cancelled",
                        principal=principal,
                        payload={
                            "state": stopped_experiment,
                            "trigger_evidence_id": str(evidence.evidence_id),
                        },
                        correlation_id=correlation_id,
                        idempotency_key=idempotency_key,
                    )
                )
                result["experiment"] = stopped_experiment
        result.update(
            {
                "_event_ids": event_ids,
                "_prior_revision": 0,
                "_new_revision": 1,
                "_resource_type": "evidence",
                "_resource_id": evidence.evidence_id,
            }
        )
        return result

    async def _mutate_evaluation_submit(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        experiment_id = _required_str(payload, "experiment_id")
        resource = ResourceScope.resource(company_id, ResourceType.EXPERIMENT, experiment_id)
        record, _ = await self._projection(tx, "experiment", experiment_id)
        experiment = dict(record["state"])
        executors = [str(item) for item in experiment["executor_principal_ids"]]
        self.policy.require_independent_evaluator(principal, resource, executed_by=executors)
        await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.EVALUATION_SUBMIT.value},
            cell_id=str(experiment["cell_id"]),
        )
        if principal.principal_id != str(experiment["evaluator_principal_id"]):
            raise SeparationOfDutiesError(
                "evaluation must be submitted by the preregistered evaluator"
            )
        if experiment.get("revision") != expected_revision:
            raise ConflictError("stale experiment revision")
        if experiment.get("state") != ExperimentStatus.EVALUATING.value:
            raise ValidationError("experiment is not awaiting evaluation")
        judged_end = parse_aware_datetime(payload.get("window_ends_at"), "window_ends_at")
        experiment_end = parse_aware_datetime(experiment["window"]["ends_at"], "window.ends_at")
        if judged_end != experiment_end:
            raise ValidationError(
                "evaluation must judge the time window against window_ends_at, "
                "not dates in hypothesis prose"
            )
        judged_window_text = _jsonable(experiment_end)
        claimed_revision = payload.get("experiment_revision")
        if claimed_revision != expected_revision:
            raise ConflictError("evaluation references a stale experiment revision")
        criteria = payload.get("criterion_results")
        if not isinstance(criteria, list) or not criteria:
            raise ValidationError("criterion_results are required")
        frozen_metrics = {str(metric["metric_id"]): metric for metric in experiment["metrics"]}
        success_by_metric: dict[str, list[Mapping[str, Any]]] = {}
        failure_by_metric: dict[str, list[Mapping[str, Any]]] = {}
        for threshold in experiment["success_thresholds"]:
            success_by_metric.setdefault(str(threshold["metric_id"]), []).append(threshold)
        for threshold in experiment["failure_thresholds"]:
            failure_by_metric.setdefault(str(threshold["metric_id"]), []).append(threshold)
        required_criteria = set(success_by_metric)
        supplied_ids = [
            str(item.get("criterion_id")) for item in criteria if isinstance(item, Mapping)
        ]
        if len(supplied_ids) != len(set(supplied_ids)) or set(supplied_ids) != required_criteria:
            raise ValidationError(
                "criterion_results must exactly cover the frozen success criteria"
            )
        computed_outcomes: list[EvaluationVerdict] = []
        for criterion in criteria:
            if not isinstance(criterion, Mapping):
                raise ValidationError("criterion result must be an object")
            criterion_id = str(criterion["criterion_id"])
            cited_records: list[Mapping[str, Any]] = []
            for evidence_id in criterion.get("evidence_ids", ()):
                evidence_record, _ = await self._projection(tx, "evidence", evidence_id)
                if evidence_record["state"].get("experiment_id") != experiment_id:
                    raise ValidationError("evaluation evidence belongs to another experiment")
                cited_records.append(evidence_record["state"])
            metric = frozen_metrics[criterion_id]
            if metric["unit"] == "count":
                observed = Decimal(len({item["evidence_id"] for item in cited_records}))
                if not cited_records and criterion.get("outcome") == EvaluationVerdict.FAILED.value:
                    fitness_ids = await self._qualifying_fitness_evidence_ids(tx, experiment_id)
                    if fitness_ids:
                        raise ValidationError(
                            "empty-evidence failure requires no qualifying fitness evidence"
                        )
            else:
                numeric_values = [
                    Decimal(str(item["observation"]["value"]))
                    for item in cited_records
                    if isinstance(item.get("observation", {}).get("value"), (int, float))
                    and not isinstance(item.get("observation", {}).get("value"), bool)
                ]
                if not numeric_values:
                    raise ValidationError("criterion has no numeric cited evidence")
                observed = max(numeric_values)
            claimed_observed = _decimal(criterion.get("observed_value"), "criterion.observed_value")
            if claimed_observed != observed:
                raise ValidationError("criterion observed_value does not match its cited evidence")
            success = all(
                _threshold_holds(
                    observed,
                    str(threshold["operator"]),
                    Decimal(str(threshold["value"])),
                )
                for threshold in success_by_metric[criterion_id]
            )
            failure = any(
                _threshold_holds(
                    observed,
                    str(threshold["operator"]),
                    Decimal(str(threshold["value"])),
                )
                for threshold in failure_by_metric.get(criterion_id, ())
            )
            computed = (
                EvaluationVerdict.FAILED
                if failure
                else EvaluationVerdict.PASSED
                if success
                else EvaluationVerdict.INCONCLUSIVE
            )
            if criterion.get("outcome") != computed.value:
                raise ValidationError("criterion outcome does not match frozen thresholds")
            computed_outcomes.append(computed)
        result = EvaluationVerdict(_required_str(payload, "result"))
        aggregate = (
            EvaluationVerdict.FAILED
            if EvaluationVerdict.FAILED in computed_outcomes
            else EvaluationVerdict.PASSED
            if all(item is EvaluationVerdict.PASSED for item in computed_outcomes)
            else EvaluationVerdict.INCONCLUSIVE
        )
        if result is not aggregate:
            raise ValidationError("evaluation result does not match the frozen criterion outcomes")
        values = dict(payload)
        values.pop("window_ends_at", None)
        values.update(
            evaluation_id=_uuid(
                payload.get("evaluation_id", uuid5(operation_id, "evaluation")),
                "evaluation_id",
            ),
            company_id=_uuid(company_id, "company_id"),
            experiment_id=_uuid(experiment_id, "experiment_id"),
            experiment_revision=expected_revision,
            evaluator_principal_id=_principal_uuid(principal),
            evaluator_run_id=self._principal_run_uuid(principal),
            executor_principal_ids={_uuid(item, "executor_principal_id") for item in executors},
            independence_attestation=IndependenceAttestation(
                is_independent=True,
                checked_by_substrate=True,
                basis=IndependenceBasis.SEPARATE_PRINCIPAL,
            ),
            result=result,
            evaluated_at=self.clock(),
        )
        evaluation = Evaluation.model_validate(values)
        await self._put_model(
            tx,
            "evaluation",
            evaluation.evaluation_id,
            evaluation,
            expected_projection_revision=0,
        )
        updated_experiment = self._revised_state(
            experiment,
            expected_revision,
            state=result.value,
            evaluated_at=_jsonable(self.clock()),
            evaluation_id=str(evaluation.evaluation_id),
        )
        await self._put_state(
            tx,
            "experiment",
            experiment_id,
            updated_experiment,
            expected_projection_revision=record["revision"],
        )
        evaluation_event = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="evaluation-submitted",
            aggregate_type="evaluation",
            aggregate_id=evaluation.evaluation_id,
            event_type="evaluation.submitted",
            principal=principal,
            payload={"state": evaluation},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        experiment_event = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="experiment-evaluated",
            aggregate_type="experiment",
            aggregate_id=experiment_id,
            event_type=f"experiment.{result.value}",
            principal=principal,
            payload={"state": updated_experiment, "evaluation_id": str(evaluation.evaluation_id)},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "evaluation": evaluation,
            "experiment": updated_experiment,
            "judged_window_ends_at": judged_window_text,
            "_event_ids": [evaluation_event, experiment_event],
            "_prior_revision": expected_revision,
            "_new_revision": expected_revision + 1,
            "_resource_type": "evaluation",
            "_resource_id": evaluation.evaluation_id,
            "_audit_details": {
                "experiment_id": experiment_id,
                "evaluation_id": str(evaluation.evaluation_id),
                "judged_window_ends_at": judged_window_text,
                "result": result.value,
            },
        }

    async def _mutate_gate_evaluate(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        self._require(principal, Capability.GATE_EVALUATE, ResourceScope.company(company_id))
        if expected_revision != 0:
            raise ConflictError("new gate decision requires expected_revision 0")
        target = payload.get("target")
        if not isinstance(target, Mapping):
            raise ValidationError("target is required")
        target_kind = _required_str(target, "kind")
        if target_kind != "cell":
            raise ValidationError("fitness gate target must be a cell")
        target_id = _required_str(target, "id")
        target_record, _ = await self._projection(tx, target_kind, target_id)
        target_cell_id = _required_str(target_record["state"], "cell_id")
        await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.GATE_EVALUATE.value},
            cell_id=target_cell_id,
        )
        if target_record["state"].get("revision") != target.get("revision"):
            raise ConflictError("gate target revision is stale")
        try:
            action = AllocationAction(_required_str(payload, "action"))
            allocation_stage = AllocationStage(_required_str(payload, "stage"))
        except ValueError as exc:
            raise ValidationError("unknown allocation action or stage") from exc
        containment_action = action in {AllocationAction.PAUSE, AllocationAction.KILL}
        if (allocation_stage is AllocationStage.CONTAINMENT) != containment_action:
            raise ValidationError("pause and kill require containment stage exactly")

        company_record, _ = await self._projection(tx, "company", company_id)
        constitution_record, _ = await self._projection(
            tx, "constitution", company_record["state"]["active_constitution_id"]
        )
        constitution = constitution_record["state"]
        fitness_policy = constitution["fitness_policy"]
        budget_policy = constitution["budget_policy"]

        evaluation: Mapping[str, Any] = {}
        experiment: Mapping[str, Any] = {}
        evidence: list[Mapping[str, Any]] = []
        evaluation_id: str | None = None
        experiment_id: str | None = None
        containment_basis: dict[str, Any] | None = None
        amount_minor = 0
        normalized_amount: dict[str, Any] | None = None
        if allocation_stage is AllocationStage.CONTAINMENT:
            current_status = target_record["state"].get(
                "status", target_record["state"].get("state")
            )
            allowed_statuses = (
                {CellStatus.ACTIVE.value}
                if action is AllocationAction.PAUSE
                else {CellStatus.ACTIVE.value, CellStatus.PAUSED.value}
            )
            if current_status not in allowed_statuses:
                raise ValidationError("cell state does not permit the containment action")
            if payload.get("amount") is not None:
                raise ValidationError("containment gate amount must be null")
            if payload.get("evaluation_id") is not None:
                raise ValidationError("containment gate does not accept an evaluation")
            basis = payload.get("containment_basis")
            if not isinstance(basis, Mapping):
                raise ValidationError("containment_basis is required")
            if set(basis) != {"kind", "summary"}:
                raise ValidationError("containment_basis fields are invalid")
            kind = _required_str(basis, "kind")
            if kind not in _CONTAINMENT_BASIS_KINDS:
                raise ValidationError("unsupported containment basis")
            if kind == "cell_owner_authorized" and action is AllocationAction.KILL:
                raise AuthorizationError("containment basis requires founder authority")
            summary = _required_str(basis, "summary")
            if len(summary) > 1_000:
                raise ValidationError("containment basis summary is too long")
            if kind == "founder_authorized":
                founder_ids = {
                    str(value)
                    for value in constitution["authority_policy"]["founder_principal_ids"]
                }
                try:
                    self.policy.require_founder_action(
                        principal,
                        ResourceScope.company(company_id),
                        action=f"allocation.{action.value}",
                    )
                except AuthorizationError:
                    raise AuthorizationError(
                        "containment basis requires founder authority"
                    ) from None
                if principal.principal_id not in founder_ids:
                    raise AuthorizationError("containment basis requires founder authority")
            elif Role.CELL_OWNER not in principal.roles:
                raise AuthorizationError("containment basis requires cell owner authority")
            elif action is not AllocationAction.PAUSE:
                raise AuthorizationError("containment basis requires founder authority")
            containment_basis = {
                "kind": kind,
                "summary": summary,
                "authorized_by_principal_id": principal.principal_id,
                "authorized_at": _jsonable(self.clock()),
            }
        else:
            if payload.get("containment_basis") is not None:
                raise ValidationError("containment_basis is only valid for containment")
            evaluation_id = _required_str(payload, "evaluation_id")
            evaluation_record, _ = await self._projection(tx, "evaluation", evaluation_id)
            evaluation = evaluation_record["state"]
            if evaluation.get("result") != EvaluationVerdict.PASSED.value:
                raise ValidationError("fitness gates require a passing evaluation")
            experiment_record, _ = await self._projection(
                tx, "experiment", evaluation["experiment_id"]
            )
            experiment = experiment_record["state"]
            experiment_id = str(evaluation["experiment_id"])
            if experiment.get("state") != ExperimentStatus.PASSED.value:
                raise ValidationError("experiment is not in a passed terminal state")
            if str(experiment.get("cell_id")) != target_cell_id:
                raise ValidationError(
                    "evaluation and experiment must belong to the allocation target cell"
                )
            evidence_records = await tx.list_projections(projection_type="evidence", limit=1_000)
            evidence = [
                record["state"]
                for record in evidence_records
                if record["state"].get("experiment_id") == experiment_id
            ]
            amount = payload.get("amount")
            if action is AllocationAction.FUND:
                if not isinstance(amount, Mapping):
                    raise ValidationError("fund gate requires a positive amount")
                _require_matching_unit(
                    amount,
                    budget_policy,
                    "gate amount unit differs from the constitution",
                )
                amount_minor = _minor_amount(amount.get("amount_minor"), "amount.amount_minor")
                if amount_minor < 1:
                    raise ValidationError("fund gate requires a positive amount")
                fund_unit, fund_currency = identity_of(amount)
                normalized_amount = dump_budget_amount(
                    unit=fund_unit,
                    amount_minor=amount_minor,
                    currency=fund_currency,
                )
            elif amount is not None:
                raise ValidationError("non-fund gate amount must be null")
        evidence_ids = [item["evidence_id"] for item in evidence]
        outcome_values = [
            int(item["observation"]["value"])
            for item in evidence
            if item.get("type") == "outcome"
            and item.get("observation", {}).get("unit") == "basis_points"
            and isinstance(item.get("observation", {}).get("value"), int)
        ]
        adverse_types = {item.get("type") for item in evidence}
        stage = allocation_stage.value
        ledger_snapshots: dict[str, dict[str, Any]] = {}
        experiment_spend_minor = 0
        if allocation_stage is not AllocationStage.CONTAINMENT:
            ledgers = await self._constraint_ledgers(
                tx,
                cell_state=target_record["state"],
                stage=stage,
            )
            ledger_snapshots = {role: _budget_snapshot(budget) for role, budget in ledgers.items()}
            if any(
                not same_ledger_identity(snapshot, budget_policy)
                for snapshot in ledger_snapshots.values()
            ):
                raise BudgetError("live ledger unit differs from the constitution")
            experiment_runtime = await tx.get_projection("experiment_runtime", str(experiment_id))
            experiment_spend_minor = int(experiment.get("actual_cost", {}).get("amount_minor", 0))
            if experiment_runtime is not None:
                reservation_group_id = experiment_runtime["state"].get(
                    "reservation_group_id",
                    experiment_runtime["state"].get("reservation_id"),
                )
                if reservation_group_id is not None:
                    experiment_group = await tx.get_reservation_group(str(reservation_group_id))
                    if experiment_group is not None and experiment_group["status"] == "committed":
                        policy_unit, _policy_currency = identity_of(budget_policy)
                        experiment_spend_minor = ledger_to_quantity(
                            experiment_group["actual_amount"],
                            policy_unit,
                            "experiment reservation actual amount",
                        )
        observations = GateObservations(
            open_legal_or_policy_block="policy_violation" in adverse_types,
            all_material_promises_supported=True,
            unresolved_unsupported_claim_event=False,
            verified_customer_outcome_bps=max(outcome_values) if outcome_values else None,
            mandatory_harm_threshold_fired=bool(adverse_types & {"complaint", "refund", "churn"}),
            unresolved_security_or_privacy_incident="security_incident" in adverse_types,
            unauthorized_data_scope=False,
            company_spend_minor=(
                ledger_snapshots["company"]["spent_minor"]
                + ledger_snapshots["company"]["reserved_minor"]
                if ledger_snapshots
                else 0
            ),
            cell_spend_minor=(
                ledger_snapshots["cell"]["spent_minor"] + ledger_snapshots["cell"]["reserved_minor"]
                if ledger_snapshots
                else 0
            ),
            experiment_spend_minor=experiment_spend_minor,
            stage_available_minor=(
                ledger_snapshots["stage"]["available_minor"] if ledger_snapshots else 0
            ),
            requested_allocation_minor=amount_minor,
            verified_delivery_quality_bps=max(outcome_values) if outcome_values else None,
            delivery_overrun_stop_rule_fired="delivery_overrun" in adverse_types,
            evidence_count=len(evidence),
            sources_valid=all(item.get("provenance_quality") != "inferred" for item in evidence),
            lineage_valid=all(item.get("lineage") for item in evidence),
            evaluation_independent=bool(
                evaluation.get("independence_attestation", {}).get("is_independent")
                and evaluation.get("independence_attestation", {}).get("checked_by_substrate")
            ),
        )
        thresholds = FitnessThresholds(
            minimum_evidence_count=int(fitness_policy["minimum_evidence_count"]),
            minimum_customer_outcome_bps=int(fitness_policy["minimum_customer_outcome_bps"]),
            minimum_delivery_quality_bps=int(fitness_policy["minimum_delivery_quality_bps"]),
            company_limit_minor=int(budget_policy["company_limit_minor"]),
            cell_limit_minor=int(budget_policy["cell_limit_minor"]),
            experiment_limit_minor=int(budget_policy["experiment_limit_minor"]),
            single_action_limit_minor=int(budget_policy["single_action_limit_minor"]),
        )
        evidence_by_gate = {gate.value: evidence_ids for gate in GateName}
        gate_results = evaluate_fitness_gates(observations, thresholds, evidence_by_gate)
        decision_id = str(uuid5(operation_id, "gate-decision"))
        decision = {
            "decision_id": decision_id,
            "company_id": company_id,
            "revision": 1,
            "action": action.value,
            "stage": stage,
            "target": dict(target),
            "amount": normalized_amount,
            "evaluation_id": evaluation_id,
            "experiment_id": experiment_id,
            "gate_results": gate_results,
            "passed": gates_are_eligible(
                gate_results,
                stage,
                containment_basis_verified=containment_basis is not None,
            ),
            "constitution_id": constitution["constitution_id"],
            "constitution_version": constitution["version"],
            "rule_version": fitness_policy["rule_version"],
            "evidence_ids": sorted(evidence_ids),
            "budget_snapshot": {
                **ledger_snapshots,
                "experiment_spend_minor": experiment_spend_minor,
            },
            "evaluated_by_principal_id": principal.principal_id,
            "evaluated_by_run_id": (
                str(self._principal_run_uuid(principal))
                if allocation_stage is not AllocationStage.CONTAINMENT
                else principal.agent_run_id
            ),
            "evaluated_at": _jsonable(self.clock()),
        }
        if containment_basis is not None:
            decision["containment_basis"] = containment_basis
        decision["input_digest"] = canonical_decision_digest(decision)
        await self._put_state(
            tx,
            "gate_decision",
            decision_id,
            decision,
            expected_projection_revision=0,
        )
        await self._put_state(
            tx,
            "decision",
            decision_id,
            decision,
            expected_projection_revision=0,
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="gates-evaluated",
            aggregate_type="gate_decision",
            aggregate_id=decision_id,
            event_type="gate.evaluated",
            principal=principal,
            payload={"state": decision},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "decision": decision,
            "_event_ids": [event_id],
            "_prior_revision": 0,
            "_new_revision": 1,
            "_resource_type": "decision",
            "_resource_id": decision_id,
        }

    async def _mutate_allocation_propose(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        self._require(
            principal,
            Capability.ALLOCATION_PROPOSE,
            ResourceScope.company(company_id),
        )
        if expected_revision != 0:
            raise ConflictError("new allocation proposal requires expected_revision 0")
        target = payload.get("target")
        if not isinstance(target, Mapping):
            raise ValidationError("target is required")
        if _required_str(target, "kind") != "cell":
            raise ValidationError("allocation proposal target must be a cell")
        target_record, _ = await self._projection(
            tx, _required_str(target, "kind"), _required_str(target, "id")
        )
        target_cell_id = _required_str(target_record["state"], "cell_id")
        await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.ALLOCATION_PROPOSE.value},
            cell_id=target_cell_id,
        )
        gate_decision_id = _required_str(payload, "gate_decision_id")
        gate_record, _ = await self._projection(tx, "gate_decision", gate_decision_id)
        gate_decision = gate_record["state"]
        company_record, _ = await self._projection(tx, "company", company_id)
        constitution_record, _ = await self._projection(
            tx, "constitution", company_record["state"]["active_constitution_id"]
        )
        constitution = constitution_record["state"]
        if (
            gate_decision.get("constitution_id") != constitution["constitution_id"]
            or gate_decision.get("constitution_version") != constitution["version"]
        ):
            raise ConflictError("active constitution changed after gate evaluation")
        if dict(target) != gate_decision.get("target"):
            raise ConflictError("allocation target differs from gate decision")
        if payload.get("action") != gate_decision.get("action") or payload.get(
            "stage"
        ) != gate_decision.get("stage"):
            raise ConflictError("allocation action or stage differs from gate decision")
        if payload.get("amount") != gate_decision.get("amount"):
            raise ConflictError("allocation amount differs from gate decision")
        stage = _required_str(payload, "stage")
        containment_basis = gate_decision.get("containment_basis")
        containment_basis_verified = _containment_basis_verified(
            stage,
            containment_basis,
            action=_required_str(payload, "action"),
        )
        if not gate_decision.get("passed") or not gates_are_eligible(
            gate_decision["gate_results"],
            stage,
            containment_basis_verified=containment_basis_verified,
        ):
            raise ValidationError("allocation is not eligible under all fitness gates")
        if target_record["state"].get("revision") != target.get("revision"):
            raise ConflictError("allocation target revision is stale")
        ranking_request = payload.get("ranking")
        if not isinstance(ranking_request, Mapping):
            raise ValidationError("ranking is required")
        try:
            if stage == AllocationStage.EXPLOITATION.value:
                ranking = compute_exploitation_ranking(ranking_request)
            elif stage == AllocationStage.EXPLORATION.value:
                ranking = compute_exploration_ranking(ranking_request)
            elif stage == AllocationStage.CONTAINMENT.value:
                ranking = containment_ranking()
            else:
                raise ValidationError("unknown allocation stage")
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        allocation_id = _uuid(
            payload.get("allocation_id", uuid5(operation_id, "allocation")),
            "allocation_id",
        )
        amount = payload.get("amount")
        proposed_at = self.clock()
        decision_input = {
            "allocation_id": str(allocation_id),
            "action": payload["action"],
            "target": dict(target),
            "stage": stage,
            "gate_results": gate_decision["gate_results"],
            "ranking": ranking,
            "amount": amount,
            "constitution_id": constitution["constitution_id"],
            "constitution_version": constitution["version"],
            "rule_version": "fitness-v1",
        }
        if containment_basis_verified:
            decision_input["containment_basis"] = dict(containment_basis)
        values = {
            "allocation_id": allocation_id,
            "company_id": _uuid(company_id, "company_id"),
            "revision": 1,
            "decision_state": AllocationStatus.PROPOSED,
            "action": payload["action"],
            "target": dict(target),
            "stage": stage,
            "gate_results": gate_decision["gate_results"],
            "ranking": ranking,
            "amount": amount,
            "constitution_id": constitution["constitution_id"],
            "constitution_version": constitution["version"],
            "rule_version": "fitness-v1",
            "input_digest": canonical_decision_digest(decision_input),
            "rationale_summary": _required_str(payload, "rationale_summary"),
            "proposed_by_principal_id": _principal_uuid(principal),
            "proposed_by_run_id": self._principal_run_uuid(principal),
            "proposed_at": proposed_at,
            "decided_by_principal_id": None,
            "decided_by_run_id": None,
            "decided_at": None,
        }
        allocation = Allocation.model_validate(values)
        await self._put_model(
            tx,
            "allocation",
            allocation_id,
            allocation,
            expected_projection_revision=0,
        )
        await self._put_state(
            tx,
            "allocation_runtime",
            allocation_id,
            {"gate_decision_id": gate_decision_id, "reservation_id": None},
            expected_projection_revision=0,
        )
        decision = {
            **allocation.model_dump(mode="json"),
            "gate_decision_id": gate_decision_id,
            "evidence_ids": gate_decision["evidence_ids"],
        }
        if containment_basis_verified:
            decision["containment_basis"] = dict(containment_basis)
        await self._put_state(
            tx,
            "decision",
            allocation_id,
            decision,
            expected_projection_revision=0,
        )
        allocation_event_payload: dict[str, Any] = {"state": allocation}
        if containment_basis_verified:
            allocation_event_payload["containment_basis"] = dict(containment_basis)
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="allocation-proposed",
            aggregate_type="allocation",
            aggregate_id=allocation_id,
            event_type="allocation.proposed",
            principal=principal,
            payload=allocation_event_payload,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        decision_event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="allocation-decision-proposed",
            aggregate_type="decision",
            aggregate_id=allocation_id,
            event_type="decision.proposed",
            principal=principal,
            payload={"state": decision},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "allocation": allocation,
            "_event_ids": [event_id, decision_event_id],
            "_prior_revision": 0,
            "_new_revision": 1,
            "_resource_type": "allocation",
            "_resource_id": allocation_id,
        }

    async def _mutate_allocation_commit(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        # Check caller authority before resolving an allocation so unauthorized
        # callers cannot use existence as an oracle.
        company_scope = ResourceScope.company(company_id)
        if not (
            self.policy.is_allowed(principal, Capability.ALLOCATION_COMMIT_BOUNDED, company_scope)
            or self.policy.is_allowed(
                principal, Capability.ALLOCATION_COMMIT_IRREVERSIBLE, company_scope
            )
        ):
            raise AuthorizationError("authorization denied")
        allocation_id = _required_str(payload, "allocation_id")
        record, parsed = await self._projection(tx, "allocation", allocation_id, Allocation)
        assert isinstance(parsed, Allocation)
        allocation = parsed
        commit_capability = (
            Capability.ALLOCATION_COMMIT_IRREVERSIBLE
            if allocation.action in {AllocationAction.KILL}
            else Capability.ALLOCATION_COMMIT_BOUNDED
        )
        self._require(principal, commit_capability, company_scope)
        target_record, target_model = await self._projection(
            tx,
            allocation.target.kind.value,
            allocation.target.id,
            OfferCell,
        )
        assert isinstance(target_model, OfferCell)
        target_cell_id = _required_str(target_record["state"], "cell_id")
        await self._assert_agent_capabilities(
            tx,
            principal,
            {commit_capability.value},
            cell_id=target_cell_id,
        )
        if allocation.revision != expected_revision:
            raise ConflictError("stale allocation revision")
        if allocation.decision_state is not AllocationStatus.PROPOSED:
            raise ValidationError("allocation is not proposed")
        if target_record["state"].get("revision") != allocation.target.revision:
            raise ConflictError("allocation target revision changed after proposal")
        company_record, _ = await self._projection(tx, "company", company_id)
        constitution_record, _ = await self._projection(
            tx, "constitution", company_record["state"]["active_constitution_id"]
        )
        constitution = constitution_record["state"]
        if (
            str(allocation.constitution_id) != constitution["constitution_id"]
            or allocation.constitution_version != constitution["version"]
        ):
            raise ConflictError("active constitution changed after proposal")
        runtime_record, _ = await self._projection(tx, "allocation_runtime", allocation_id)
        runtime = dict(runtime_record["state"])
        gate_record, _ = await self._projection(tx, "gate_decision", runtime["gate_decision_id"])
        gate_decision = gate_record["state"]
        if (
            gate_decision.get("constitution_id") != constitution["constitution_id"]
            or gate_decision.get("constitution_version") != constitution["version"]
        ):
            raise ConflictError("active constitution changed after gate evaluation")
        containment_basis = gate_decision.get("containment_basis")
        containment_basis_verified = _containment_basis_verified(
            allocation.stage.value,
            containment_basis,
            action=allocation.action.value,
            founder_principal_ids={
                str(value) for value in constitution["authority_policy"]["founder_principal_ids"]
            },
        )
        if not gate_decision.get("passed") or not gates_are_eligible(
            gate_decision["gate_results"],
            allocation.stage.value,
            containment_basis_verified=containment_basis_verified,
        ):
            raise ValidationError("fitness gates no longer permit allocation")
        reservation = None
        if allocation.amount is not None and allocation.amount.amount_minor > 0:
            cell_record, _ = await self._projection(tx, "cell", allocation.target.id)
            ledgers = await self._constraint_ledgers(
                tx,
                cell_state=cell_record["state"],
                stage=allocation.stage.value,
            )
            ledger_snapshots = {role: _budget_snapshot(budget) for role, budget in ledgers.items()}
            budget_policy = constitution["budget_policy"]
            if not same_ledger_identity(allocation.amount.model_dump(mode="json"), budget_policy):
                raise BudgetError("allocation unit differs from the live ledgers")
            if allocation.amount.amount_minor > int(budget_policy["single_action_limit_minor"]):
                raise BudgetError("allocation exceeds the constitution action limit")
            company_exposure = (
                ledger_snapshots["company"]["spent_minor"]
                + ledger_snapshots["company"]["reserved_minor"]
            )
            cell_exposure = (
                ledger_snapshots["cell"]["spent_minor"] + ledger_snapshots["cell"]["reserved_minor"]
            )
            experiment_spend_minor = int(
                gate_decision.get("budget_snapshot", {}).get("experiment_spend_minor", 0)
            )
            amount_minor = allocation.amount.amount_minor
            if company_exposure + amount_minor > int(budget_policy["company_limit_minor"]):
                raise BudgetError("allocation exceeds the live company budget")
            if cell_exposure + amount_minor > int(budget_policy["cell_limit_minor"]):
                raise BudgetError("allocation exceeds the live cell budget")
            if experiment_spend_minor + amount_minor > int(budget_policy["experiment_limit_minor"]):
                raise BudgetError("allocation exceeds the experiment funding limit")
            if amount_minor > ledger_snapshots["stage"]["available_minor"]:
                raise BudgetError("allocation exceeds the live stage budget")
            reservation_group_id = str(uuid5(_uuid(allocation_id, "allocation_id"), "reservation"))
            reservation = await tx.reserve_budget_group(
                reservation_group_id=reservation_group_id,
                budget_ids={role: budget["budget_id"] for role, budget in ledgers.items()},
                amount=quantity_to_ledger(
                    allocation.amount.amount_minor, allocation.amount.unit.value
                ),
                purpose="committed portfolio allocation",
                expected_revisions={
                    budget["budget_id"]: budget["revision"] for budget in ledgers.values()
                },
                idempotency_key=idempotency_key,
                metadata={
                    "allocation_id": allocation_id,
                    "stage": allocation.stage.value,
                    "amount_minor": allocation.amount.amount_minor,
                },
                now=self.clock(),
            )
            runtime.update(
                reservation_id=reservation_group_id,
                reservation_group_id=reservation_group_id,
                reservation_ids=reservation["reservation_ids"],
                budget_ids=reservation["budget_ids"],
                budget_commit_snapshot=ledger_snapshots,
            )
        decided_at = self.clock()
        contained_cell: OfferCell | None = None
        if allocation.stage is AllocationStage.CONTAINMENT:
            containment_status = (
                CellStatus.PAUSED
                if allocation.action is AllocationAction.PAUSE
                else CellStatus.KILLED
            )
            contained_cell = transition_offer_cell(
                target_model,
                containment_status,
                expected_revision=allocation.target.revision,
                at=decided_at,
            )
        values = allocation.model_dump(mode="python", round_trip=True)
        decided_by_run_id = (
            _uuid(principal.agent_run_id, "verified agent_run_id")
            if principal.agent_run_id is not None
            else None
        )
        if decided_by_run_id is None:
            if allocation.action is not AllocationAction.KILL:
                raise AuthorizationError("an active server-derived agent run is required")
            self.policy.require_founder_action(
                principal,
                company_scope,
                action="allocation.kill",
            )
        values.update(
            revision=allocation.revision + 1,
            decision_state=AllocationStatus.COMMITTED,
            decided_by_principal_id=_principal_uuid(principal),
            decided_by_run_id=decided_by_run_id,
            decided_at=decided_at,
        )
        committed = Allocation.model_validate(values)
        await self._put_model(
            tx,
            "allocation",
            allocation_id,
            committed,
            expected_projection_revision=record["revision"],
        )
        await self._put_state(
            tx,
            "allocation_runtime",
            allocation_id,
            runtime,
            expected_projection_revision=runtime_record["revision"],
        )
        if contained_cell is not None:
            await self._put_model(
                tx,
                "cell",
                target_cell_id,
                contained_cell,
                expected_projection_revision=target_record["revision"],
            )
        decision = {
            **committed.model_dump(mode="json"),
            "gate_decision_id": runtime["gate_decision_id"],
            "evidence_ids": gate_decision["evidence_ids"],
        }
        if containment_basis_verified:
            decision["containment_basis"] = dict(containment_basis)
        decision_record, _ = await self._projection(tx, "decision", allocation_id)
        await self._put_state(
            tx,
            "decision",
            allocation_id,
            decision,
            expected_projection_revision=decision_record["revision"],
        )
        allocation_event_payload = {"state": committed}
        if containment_basis_verified:
            allocation_event_payload["containment_basis"] = dict(containment_basis)
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="allocation-committed",
            aggregate_type="allocation",
            aggregate_id=allocation_id,
            event_type="allocation.committed",
            principal=principal,
            payload=allocation_event_payload,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        decision_event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="allocation-decision-committed",
            aggregate_type="decision",
            aggregate_id=allocation_id,
            event_type="decision.committed",
            principal=principal,
            payload={"state": decision},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        event_ids = [event_id, decision_event_id]
        if contained_cell is not None:
            event_ids.append(
                await self._append_event(
                    tx,
                    operation_id=operation_id,
                    suffix="allocation-containment-cell",
                    aggregate_type="cell",
                    aggregate_id=target_cell_id,
                    event_type=f"cell.{contained_cell.status.value}",
                    principal=principal,
                    payload={
                        "state": contained_cell,
                        "allocation_id": allocation_id,
                        "containment_basis": dict(containment_basis),
                    },
                    correlation_id=correlation_id,
                    idempotency_key=idempotency_key,
                )
            )
        return {
            "allocation": committed,
            "reservation": reservation,
            **({"cell": contained_cell} if contained_cell is not None else {}),
            "_event_ids": event_ids,
            "_prior_revision": expected_revision,
            "_new_revision": expected_revision + 1,
            "_resource_type": "allocation",
            "_resource_id": allocation_id,
        }

    async def _mutate_cell_create(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        self._require(principal, Capability.CELL_CREATE, ResourceScope.company(company_id))
        if expected_revision != 0:
            raise ConflictError("new cell creation requires expected_revision 0")
        at = self.clock()
        cell_id = _uuid(payload.get("cell_id", uuid5(operation_id, "cell")), "cell_id")
        await self._assert_agent_capabilities(
            tx, principal, {Capability.CELL_CREATE.value}, cell_id=str(cell_id)
        )
        company_record, _ = await self._projection(tx, "company", company_id)
        constitution_record, _ = await self._projection(
            tx,
            "constitution",
            company_record["state"]["active_constitution_id"],
        )
        budget_policy = constitution_record["state"]["budget_policy"]
        policy_unit, policy_currency = identity_of(budget_policy)
        if "unit" in payload or "currency" in payload:
            _require_matching_unit(
                payload,
                budget_policy,
                "cell budget unit must match the constitution budget policy",
            )
        cell_unit, cell_currency = policy_unit, policy_currency
        cell_limit_minor = parse_limit_to_minor(
            payload.get("budget_limit"), cell_unit, "budget_limit"
        )
        if cell_limit_minor > int(budget_policy["cell_limit_minor"]):
            raise BudgetError("cell budget exceeds the constitution cell limit")
        company_budget = await self._one_active_budget(tx, scope=BudgetScope.COMPANY.value)
        if cell_limit_minor > ledger_to_quantity(
            company_budget["limit"], cell_unit, "company budget limit"
        ):
            raise BudgetError("cell budget exceeds the company ledger limit")
        budget_id = _uuid(payload.get("budget_id", uuid5(operation_id, "budget")), "budget_id")
        budget = await tx.create_budget(
            budget_id=str(budget_id),
            scope=BudgetScope.CELL.value,
            parent_budget_id=company_budget["budget_id"],
            cell_id=str(cell_id),
            limit=quantity_to_ledger(cell_limit_minor, cell_unit),
            unit=cell_unit,
            currency=cell_currency,
            starts_at=at,
            ends_at=at + timedelta(days=int(payload.get("budget_duration_days", 90))),
            metadata={"ledger_kind": "constraint"},
            now=at,
        )
        cell = OfferCell(
            cell_id=cell_id,
            company_id=_uuid(company_id, "company_id"),
            name=_required_str(payload, "name"),
            objective=_required_str(payload, "objective"),
            owner_principal_id=_principal_uuid(principal),
            budget_id=budget_id,
            state=CellStatus.DRAFT,
            created_at=at,
            updated_at=at,
        )
        await self._put_model(tx, "cell", cell_id, cell, expected_projection_revision=0)
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="cell",
            aggregate_type="cell",
            aggregate_id=cell_id,
            event_type="cell.created",
            principal=principal,
            payload={"state": cell, "budget": budget},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "cell": cell,
            "budget": budget,
            "_event_ids": [event_id],
            "_prior_revision": 0,
            "_new_revision": 1,
            "_resource_type": "cell",
            "_resource_id": cell_id,
        }

    async def _mutate_maintenance_run(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        """On-demand maintenance scan; nothing schedules it.

        Founder: company-wide (or one cell with ``cell_id``).  Cell owner: its
        own cell only.  With ``end_runs_on_killed_cells: true`` it ends agent
        runs still running on killed cells in scope (runs left over from kills
        recorded before cell.transition ended them); otherwise it only reports.
        """

        company_id = _principal_company(principal)
        unknown = set(payload) - {"cell_id", "end_runs_on_killed_cells"}
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValidationError(f"maintenance.run payload contains unsupported fields: {names}")
        cell_id = _optional_str(payload, "cell_id", max_length=256)
        end_runs = payload.get("end_runs_on_killed_cells", False)
        if not isinstance(end_runs, bool):
            raise ValidationError("end_runs_on_killed_cells must be a boolean")
        resource = (
            ResourceScope.resource(company_id, ResourceType.CELL, cell_id)
            if cell_id is not None
            else ResourceScope.company(company_id)
        )
        self._require(principal, Capability.MAINTENANCE_RUN, resource)
        await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.MAINTENANCE_RUN.value},
            cell_id=cell_id,
            require_bound_cell_target=True,
        )
        if cell_id is not None:
            await self._projection(tx, "cell", cell_id)
        readonly = ReadOnlyTransaction(tx)
        now = self.clock()
        report = await self._build_maintenance_report(
            readonly,
            company_id=company_id,
            now=now,
            agent_runs=await scan_running_agent_runs(readonly),
            scan_cap=DEFAULT_PROJECTION_SCAN_CAP,
        )
        if cell_id is not None:
            report = filter_report_for_cell(report, cell_id)
        ended: dict[str, list[str]] = {}
        killed_scan_truncated = False
        if end_runs:
            if cell_id is not None:
                record = await tx.get_projection("cell", cell_id)
                state = dict(record["state"]) if record is not None else {}
                killed_cells = (
                    [cell_id] if state.get("status", state.get("state")) == "killed" else []
                )
            else:
                cell_scan = await scan_projections(readonly, "cell")
                killed_scan_truncated = cell_scan.truncated
                killed_cells = sorted(
                    str(item["state"]["cell_id"])
                    for item in cell_scan.records
                    if item["state"].get("status", item["state"].get("state")) == "killed"
                )
            for killed in killed_cells:
                run_ids = await tx.end_cell_agent_runs(
                    cell_id=killed,
                    termination_code=CELL_KILLED_TERMINATION_CODE,
                    ended_at=now,
                )
                if run_ids:
                    ended[killed] = run_ids
        ended_ids = sorted(run_id for ids in ended.values() for run_id in ids)
        return {
            "report": report,
            "end_runs_on_killed_cells": end_runs,
            "ended_agent_run_ids": ended_ids,
            "ended_agent_run_ids_by_cell": ended,
            "killed_cell_scan_truncated": killed_scan_truncated,
            "_event_ids": [],
            "_prior_revision": 0,
            "_new_revision": 0,
            "_resource_type": "cell" if cell_id is not None else "company",
            "_resource_id": cell_id or company_id,
            "_audit_details": {
                "end_runs_on_killed_cells": end_runs,
                "ended_agent_run_ids": ended_ids,
                "ended_agent_run_ids_by_cell": ended,
                "killed_cell_scan_truncated": killed_scan_truncated,
                "report_counts": report.get("counts", {}),
                "report_truncated": report.get("truncated", False),
                **({"cell_id": cell_id} if cell_id is not None else {}),
            },
        }

    async def _mutate_cell_transition(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        cell_id = _required_str(payload, "cell_id")
        target = CellStatus(_required_str(payload, "target_status"))
        capability = Capability.CELL_TRANSITION_OPERATIONAL
        if target is CellStatus.PAUSED:
            capability = Capability.CELL_TRANSITION_PAUSE
        if target in {CellStatus.SCALED, CellStatus.KILLED}:
            capability = Capability.CELL_TRANSITION_IRREVERSIBLE
        resource = ResourceScope.resource(company_id, ResourceType.CELL, cell_id)
        reason: str | None = None
        founder_decision_ref: str | None = None
        kill_authority: str | None = None
        if target is CellStatus.KILLED:
            reason = _optional_str(payload, "reason", max_length=2_000)
            founder_decision_ref = _optional_str(payload, "founder_decision_ref", max_length=500)
            if self.policy.is_allowed(principal, Capability.CELL_TRANSITION_IRREVERSIBLE, resource):
                kill_authority = "founder"
            elif founder_decision_ref is not None:
                # Owner-signed kill: the cell owner executes a recorded founder
                # decision.  Without founder_decision_ref the founder-only gate
                # below still refuses the cell owner.
                capability = Capability.CELL_TRANSITION_KILL_OWNER_SIGNED
                kill_authority = "cell_owner_founder_decision_ref"
        self._require(principal, capability, resource)
        await self._assert_agent_capabilities(tx, principal, {"cell.transition"}, cell_id=cell_id)
        if target is CellStatus.SCALED:
            raise ValidationError(
                "cell scaling is unavailable until it is atomically bound to a verified "
                "fitness-gate decision"
            )
        record, model = await self._projection(tx, "cell", cell_id, OfferCell)
        assert isinstance(model, OfferCell)
        if model.status is CellStatus.PAUSED and target is CellStatus.ACTIVE:
            raise ValidationError(
                "cell reactivation is unavailable until remediation, approval, and budget "
                "checks are atomically bound"
            )
        if target is CellStatus.READY and not model.offer_version_ids:
            raise ValidationError("a cell requires an offer before becoming ready")
        if kill_authority == "cell_owner_founder_decision_ref":
            await self._assert_owner_signed_kill_quiescent(tx, cell_id)
        updated = transition_offer_cell(
            model, target, expected_revision=expected_revision, at=self.clock()
        )
        await self._put_model(
            tx,
            "cell",
            cell_id,
            updated,
            expected_projection_revision=record["revision"],
        )
        ended_run_ids: list[str] = []
        if target is CellStatus.KILLED:
            # Kill ends the cell's live agent runs in this same transaction, so
            # no run outlives its cell (accurate by construction, no sweeper).
            ended_run_ids = await tx.end_cell_agent_runs(
                cell_id=cell_id,
                termination_code=CELL_KILLED_TERMINATION_CODE,
                ended_at=self.clock(),
            )
        event_payload: dict[str, Any] = {"prior_status": model.status.value, "state": updated}
        if kill_authority is not None:
            # Bounded: the event payload is capped at 32 KiB, so a heavily
            # populated cell must still be killable. The full id list lives in
            # the audit row and the tool result.
            event_payload["ended_agent_run_count"] = len(ended_run_ids)
            event_payload["ended_agent_run_ids"] = ended_run_ids[:KILL_EVENT_RUN_ID_LIMIT]
            event_payload["ended_agent_run_ids_truncated"] = (
                len(ended_run_ids) > KILL_EVENT_RUN_ID_LIMIT
            )
            event_payload["kill_authority"] = kill_authority
            if reason is not None:
                event_payload["reason"] = reason
            if founder_decision_ref is not None:
                event_payload["founder_decision_ref"] = founder_decision_ref
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="cell-transition",
            aggregate_type="cell",
            aggregate_id=cell_id,
            event_type=f"cell.{target.value}",
            principal=principal,
            payload=event_payload,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        result: dict[str, Any] = {
            "cell": updated,
            "_event_ids": [event_id],
            "_prior_revision": model.revision,
            "_new_revision": updated.revision,
            "_resource_type": "cell",
            "_resource_id": cell_id,
        }
        if kill_authority is None:
            return result
        result["ended_agent_run_ids"] = ended_run_ids
        if kill_authority == "founder":
            founder_details: dict[str, Any] = {
                "kill_authority": kill_authority,
                "ended_agent_run_ids": ended_run_ids,
            }
            if reason is not None:
                founder_details["reason"] = reason
            if founder_decision_ref is not None:
                founder_details["founder_decision_ref"] = founder_decision_ref
            result["_audit_details"] = founder_details
            return result
        now = self.clock()
        decision_id = uuid5(operation_id, "decision")
        decision: dict[str, Any] = {
            "decision_id": str(decision_id),
            "kind": "cell.kill",
            "cell_id": cell_id,
            "kill_authority": kill_authority,
            "prior_status": model.status.value,
            "new_status": target.value,
            "killed_by_principal_id": principal.principal_id,
            "killed_at": _jsonable(now),
            "cell_revision": updated.revision,
        }
        if reason is not None:
            decision["reason"] = reason
        if founder_decision_ref is not None:
            decision["founder_decision_ref"] = founder_decision_ref
        await self._put_state(
            tx,
            "decision",
            decision_id,
            decision,
            expected_projection_revision=0,
        )
        decision_event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="cell-kill-decision",
            aggregate_type="decision",
            aggregate_id=decision_id,
            event_type="decision.recorded",
            principal=principal,
            payload={"state": decision},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        result["decision"] = decision
        result["_event_ids"] = [event_id, decision_event_id]
        result["_audit_details"] = {
            key: decision[key]
            for key in ("kill_authority", "reason", "founder_decision_ref", "prior_status")
            if key in decision
        } | {"decision_id": str(decision_id), "ended_agent_run_ids": ended_run_ids}
        return result

    async def _assert_owner_signed_kill_quiescent(self, tx: StoreTransaction, cell_id: str) -> None:
        """Owner-signed kill refuses a cell that still has live experiments or work."""

        open_experiments = 0
        for record in await tx.list_projections(projection_type="experiment", limit=1_000):
            state = record["state"]
            if str(state.get("cell_id") or "") != cell_id:
                continue
            status = str(state.get("state") or state.get("status") or "")
            if status in {"registered", "running", "evaluating"}:
                open_experiments += 1
        open_work = 0
        for record in await tx.list_projections(projection_type="work_item", limit=1_000):
            state = record["state"]
            if str(state.get("cell_id") or "") != cell_id:
                continue
            if state.get("status") in {WorkStatus.OPEN.value, WorkStatus.CLAIMED.value}:
                open_work += 1
        if open_experiments or open_work:
            raise ValidationError(
                "owner-signed kill requires no open experiments and no open or claimed "
                f"work (open experiments: {open_experiments}, open or claimed work: "
                f"{open_work}); close them first or have the founder kill the cell"
            )

    async def _mutate_cell_amend(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        cell_id = _required_str(payload, "cell_id")
        self._require(
            principal,
            Capability.CELL_AMEND,
            ResourceScope.resource(company_id, ResourceType.CELL, cell_id),
        )
        record, model = await self._projection(tx, "cell", cell_id, OfferCell)
        assert isinstance(model, OfferCell)
        if model.revision != expected_revision:
            raise ConflictError("stale cell revision")
        current_status = model.state
        if current_status is not CellStatus.ACTIVE:
            raise ValidationError(
                f"only an active non-terminal cell can be amended "
                f"(current state: {current_status.value})"
            )
        await self._assert_agent_capabilities(
            tx,
            principal,
            {Capability.CELL_AMEND.value},
            cell_id=cell_id,
        )
        founder_decision_ref = _required_str(payload, "founder_decision_ref")
        if len(founder_decision_ref) > 500:
            raise ValidationError("founder_decision_ref is too long")

        allowed = {"cell_id", "objective", "founder_decision_ref"}
        frozen = {
            "name",
            "budget",
            "budget_id",
            "budget_limit",
            "owner",
            "owner_principal_id",
            "state",
            "status",
            "stage",
            "company_id",
            "revision",
            "offer_version_ids",
            "authority_policy",
            "constitution",
            "ready_at",
            "activated_at",
            "closed_at",
        }
        unknown = set(payload) - allowed
        if unknown:
            blocked = unknown & frozen
            names = ", ".join(sorted(blocked or unknown))
            if blocked:
                raise ValidationError(
                    "cell.amend cannot change cell budget, stage, owner, "
                    f"authority, or constitution: {names}"
                )
            raise ValidationError(f"cell.amend payload contains unsupported fields: {names}")

        if "objective" not in payload:
            raise ValidationError("cell.amend requires a change to objective")
        objective = _required_str(payload, "objective")
        if len(objective) > 4_000:
            raise ValidationError("objective exceeds 4000 characters")
        current_objective = str(model.objective)
        if objective == current_objective:
            raise ValidationError("cell.amend requires a change to objective")

        now = self.clock()
        prior_snapshot = {"objective": current_objective}
        new_snapshot = {"objective": objective}
        amendment = {
            "amended_at": _jsonable(now),
            "amended_by_principal_id": principal.principal_id,
            "founder_decision_ref": founder_decision_ref,
            "prior": prior_snapshot,
            "new": new_snapshot,
            "changed_fields": ["objective"],
        }
        history = list(record["state"].get("amendment_history") or ())
        history.append(amendment)
        values = model.model_dump(mode="python", round_trip=True)
        values.update(
            objective=objective,
            amendment_history=history,
            revision=model.revision + 1,
            updated_at=now,
        )
        updated = OfferCell.model_validate(values)
        await self._put_model(
            tx,
            "cell",
            cell_id,
            updated,
            expected_projection_revision=record["revision"],
        )
        decision_id = uuid5(operation_id, "decision")
        decision = {
            "decision_id": str(decision_id),
            "kind": "cell.amend",
            "cell_id": cell_id,
            "founder_decision_ref": founder_decision_ref,
            "prior": prior_snapshot,
            "new": new_snapshot,
            "changed_fields": ["objective"],
            "amended_by_principal_id": principal.principal_id,
            "amended_at": _jsonable(now),
            "cell_revision": expected_revision + 1,
        }
        await self._put_state(
            tx,
            "decision",
            decision_id,
            decision,
            expected_projection_revision=0,
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="cell-amended",
            aggregate_type="cell",
            aggregate_id=cell_id,
            event_type="cell.amended",
            principal=principal,
            payload={
                "state": updated,
                "founder_decision_ref": founder_decision_ref,
                "prior": prior_snapshot,
                "new": new_snapshot,
                "changed_fields": ["objective"],
            },
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        decision_event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="cell-amend-decision",
            aggregate_type="decision",
            aggregate_id=decision_id,
            event_type="decision.recorded",
            principal=principal,
            payload={"state": decision},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        cell_state = _jsonable(updated)
        cell_state["status"] = updated.status.value
        return {
            "cell": cell_state,
            "decision": decision,
            "amendment": amendment,
            "_event_ids": [event_id, decision_event_id],
            "_prior_revision": expected_revision,
            "_new_revision": expected_revision + 1,
            "_resource_type": "cell",
            "_resource_id": cell_id,
            "_audit_details": {
                "founder_decision_ref": founder_decision_ref,
                "changed_fields": ["objective"],
                "prior": prior_snapshot,
                "new": new_snapshot,
                "decision_id": str(decision_id),
            },
        }

    async def _mutate_offer_register(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        cell_id = _required_str(payload, "cell_id")
        self._require(
            principal,
            Capability.OFFER_REGISTER,
            ResourceScope.resource(company_id, ResourceType.CELL, cell_id),
        )
        await self._assert_agent_capabilities(
            tx, principal, {Capability.OFFER_REGISTER.value}, cell_id=cell_id
        )
        cell_record, cell_model = await self._projection(tx, "cell", cell_id, OfferCell)
        assert isinstance(cell_model, OfferCell)
        self._check_revision(cell_model, expected_revision)
        at = self.clock()
        offer_version_id = _uuid(
            payload.get("offer_version_id", uuid5(operation_id, "offer-version")),
            "offer_version_id",
        )
        offer_values = dict(payload)
        offer_values.update(
            offer_version_id=offer_version_id,
            offer_id=_uuid(payload.get("offer_id", uuid5(operation_id, "offer")), "offer_id"),
            company_id=_uuid(company_id, "company_id"),
            cell_id=_uuid(cell_id, "cell_id"),
            version=1,
            parent_offer_version_id=None,
            mutation_summary=None,
            registered_at=at,
            registered_by_principal_id=_principal_uuid(principal),
        )
        offer = Offer.model_validate(offer_values)
        await self._assert_offer_price_floor(tx, company_id, offer)
        cell_values = cell_model.model_dump(mode="python", round_trip=True)
        cell_values.update(
            offer_version_ids=cell_model.offer_version_ids | {offer_version_id},
            revision=cell_model.revision + 1,
            updated_at=at,
        )
        updated_cell = OfferCell.model_validate(cell_values)
        await self._put_model(tx, "offer", offer_version_id, offer, expected_projection_revision=0)
        await self._put_model(
            tx,
            "cell",
            cell_id,
            updated_cell,
            expected_projection_revision=cell_record["revision"],
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="offer",
            aggregate_type="offer",
            aggregate_id=offer_version_id,
            event_type="offer.registered",
            principal=principal,
            payload={"state": offer, "cell_revision": updated_cell.revision},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "offer": offer,
            "cell": updated_cell,
            "_event_ids": [event_id],
            "_prior_revision": cell_model.revision,
            "_new_revision": updated_cell.revision,
            "_resource_type": "offer",
            "_resource_id": offer_version_id,
        }

    async def _mutate_offer_mutate(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        parent_id = _required_str(payload, "parent_offer_version_id")
        parent_record, parent_model = await self._projection(tx, "offer", parent_id, Offer)
        del parent_record
        assert isinstance(parent_model, Offer)
        parent_revision = parent_model.version - 1
        if parent_revision != expected_revision:
            raise ConflictError(
                f"stale aggregate revision: expected {expected_revision}, current {parent_revision}"
            )
        cell_id = str(parent_model.cell_id)
        self._require(
            principal,
            Capability.OFFER_MUTATE,
            ResourceScope.resource(company_id, ResourceType.OFFER, parent_id),
        )
        await self._assert_agent_capabilities(
            tx, principal, {Capability.OFFER_MUTATE.value}, cell_id=cell_id
        )
        changes = payload.get("changes")
        if not isinstance(changes, Mapping) or not changes:
            raise ValidationError("changes must contain at least one material offer mutation")
        permitted = {
            "target_segment",
            "urgent_problem",
            "promised_outcome",
            "proof",
            "price",
            "commercial_model",
            "acquisition_channel",
            "delivery_recipe",
            "time_to_value_days",
            "risk_reversal",
        }
        if unknown := set(changes) - permitted:
            names = ", ".join(sorted(unknown))
            raise ValidationError(f"unsupported offer mutation fields: {names}")
        at = self.clock()
        values = parent_model.model_dump(mode="python", round_trip=True)
        values.update(changes)
        values.update(
            offer_version_id=_uuid(
                payload.get("offer_version_id", uuid5(operation_id, "offer-version")),
                "offer_version_id",
            ),
            offer_id=parent_model.offer_id,
            version=parent_model.version + 1,
            parent_offer_version_id=parent_model.offer_version_id,
            mutation_summary=_required_str(payload, "mutation_summary"),
            registered_at=at,
            registered_by_principal_id=_principal_uuid(principal),
        )
        child = Offer.model_validate(values)
        await self._assert_offer_price_floor(tx, company_id, child)
        await self._put_model(
            tx, "offer", child.offer_version_id, child, expected_projection_revision=0
        )
        cell_record, cell_model = await self._projection(tx, "cell", cell_id, OfferCell)
        assert isinstance(cell_model, OfferCell)
        cell_values = cell_model.model_dump(mode="python", round_trip=True)
        cell_values.update(
            offer_version_ids=cell_model.offer_version_ids | {child.offer_version_id},
            revision=cell_model.revision + 1,
            updated_at=at,
        )
        updated_cell = OfferCell.model_validate(cell_values)
        await self._put_model(
            tx,
            "cell",
            cell_id,
            updated_cell,
            expected_projection_revision=cell_record["revision"],
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="offer-mutation",
            aggregate_type="offer",
            aggregate_id=child.offer_version_id,
            event_type="offer.mutated",
            principal=principal,
            payload={
                "state": child,
                "parent_offer_id": parent_id,
                "changed_fields": sorted(changes),
            },
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "offer": child,
            "cell": updated_cell,
            "parent_offer_id": parent_id,
            "_event_ids": [event_id],
            "_prior_revision": parent_revision,
            "_new_revision": child.version - 1,
            "_resource_type": "offer",
            "_resource_id": child.offer_version_id,
        }

    async def _mutate_learning_propose(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        company_id = _principal_company(principal)
        self._require(
            principal,
            Capability.LEARNING_PROPOSE,
            ResourceScope.company(company_id),
        )
        cell_id = _required_str(payload, "cell_id")
        self._require(
            principal,
            Capability.LEARNING_PROPOSE,
            ResourceScope.resource(company_id, ResourceType.CELL, cell_id),
        )
        await self._assert_agent_capabilities(
            tx, principal, {Capability.LEARNING_PROPOSE.value}, cell_id=cell_id
        )
        if expected_revision != 0:
            raise ConflictError("new learning candidate requires expected_revision 0")
        experiment_id = _required_str(payload, "source_experiment_id")
        evaluation_id = _required_str(payload, "source_evaluation_id")
        experiment_record, _ = await self._projection(tx, "experiment", experiment_id)
        if str(experiment_record["state"].get("cell_id")) != cell_id:
            raise ValidationError("source experiment does not belong to the named cell")
        evaluation_record, _ = await self._projection(tx, "evaluation", evaluation_id)
        if str(evaluation_record["state"].get("experiment_id")) != experiment_id:
            raise ValidationError("source evaluation does not belong to the named experiment")
        evidence_ids = payload.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise ValidationError("evidence_ids must contain at least one evidence event")
        cited: set[UUID] = set()
        for raw_id in evidence_ids:
            evidence_id = _uuid(raw_id, "evidence_id")
            evidence_record, _ = await self._projection(tx, "evidence", evidence_id)
            if str(evidence_record["state"].get("cell_id")) != cell_id:
                raise ValidationError("cited evidence does not belong to the named cell")
            cited.add(evidence_id)
        at = self.clock()
        candidate = LearningCandidate(
            learning_id=_uuid(
                payload.get("learning_id", uuid5(operation_id, "learning")),
                "learning_id",
            ),
            company_id=_uuid(company_id, "company_id"),
            cell_id=_uuid(cell_id, "cell_id"),
            source_experiment_id=_uuid(experiment_id, "source_experiment_id"),
            source_evaluation_id=_uuid(evaluation_id, "source_evaluation_id"),
            statement=_required_str(payload, "statement"),
            confidence_bps=int(payload.get("confidence_bps", 0)),
            proposed_by_principal_id=_principal_uuid(principal),
            evidence_ids=frozenset(cited),
            status=LearningStatus.CANDIDATE,
            created_at=at,
            updated_at=at,
        )
        await self._put_model(
            tx, "learning", candidate.learning_id, candidate, expected_projection_revision=0
        )
        event_id = await self._append_event(
            tx,
            operation_id=operation_id,
            suffix="learning-proposed",
            aggregate_type="learning",
            aggregate_id=candidate.learning_id,
            event_type="learning.proposed",
            principal=principal,
            payload={"state": candidate},
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
        )
        return {
            "learning": candidate,
            "_event_ids": [event_id],
            "_prior_revision": 0,
            "_new_revision": 1,
            "_resource_type": "learning",
            "_resource_id": candidate.learning_id,
        }

    async def _mutate_learning_promote(
        self,
        tx: StoreTransaction,
        principal: VerifiedPrincipal,
        payload: dict[str, Any],
        expected_revision: int,
        operation_id: UUID,
        correlation_id: UUID,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        del expected_revision, operation_id, correlation_id, idempotency_key
        company_id = _principal_company(principal)
        self._require(
            principal,
            Capability.LEARNING_PROMOTE,
            ResourceScope.company(company_id),
        )
        learning_id = payload.get("learning_id")
        if isinstance(learning_id, str) and learning_id.strip():
            record = await tx.get_projection("learning", learning_id.strip())
            if record is not None:
                proposer = str(record["state"].get("proposed_by_principal_id"))
                if proposer == principal.principal_id:
                    raise SeparationOfDutiesError(
                        "memory cannot promote its own learning candidate"
                    )
        # Replay, transfer test, controlled trial, and rollback projection are
        # not yet bound. Persist nothing and do not claim a promotion.
        raise AuthorizationError("learning promotion gate is incomplete")


def create_service(
    *,
    store: Repository | None = None,
    database_url: str | None = None,
    clock: Clock | None = None,
    definitions_path: str | None = None,
) -> SubstrateService:
    """Create the deterministic service; agent definitions stay external data."""

    del definitions_path
    if store is not None and database_url is not None:
        raise ValueError("pass either store or database_url, not both")
    if database_url is not None:
        from .postgres import PostgresRepository

        store = PostgresRepository(database_url)
    return SubstrateService(store or InMemoryStore(), clock=clock)


__all__ = [
    "CELL_OWNER_OPERATIONAL_TOOLS",
    "MUTATION_TOOLS",
    "READ_TOOLS",
    "RUNTIME_TICK_PRINCIPAL_ID",
    "SubstrateService",
    "create_service",
    "runtime_tick_principal",
]


def _scanned_by_cell(
    *,
    cells: Sequence[Mapping[str, Any]],
    work_items: Sequence[Mapping[str, Any]],
    experiments: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    agent_runs: Sequence[Any] | None,
) -> dict[str, dict[str, int | None]]:
    """Per-cell scan counts so cell-scoped readers never see company totals."""

    out: dict[str, dict[str, int | None]] = {}

    def bump(cell_id: Any, key: str) -> None:
        if not cell_id:
            return
        bucket = out.setdefault(
            str(cell_id),
            {
                "cells": 0,
                "work_items": 0,
                "experiments": 0,
                "evidence": 0,
                "agent_runs": 0 if agent_runs is not None else None,
            },
        )
        bucket[key] = (bucket[key] or 0) + 1

    for cell in cells:
        bump(cell.get("cell_id"), "cells")
    for item in work_items:
        bump(item.get("cell_id"), "work_items")
    experiment_cell: dict[str, Any] = {}
    for state in experiments:
        experiment_cell[str(state.get("experiment_id"))] = state.get("cell_id")
        bump(state.get("cell_id"), "experiments")
    for record in evidence:
        # Scan records are projections; ownership lives in ``state``.
        state = record.get("state") if isinstance(record.get("state"), Mapping) else record
        cell_id = state.get("cell_id") or experiment_cell.get(str(state.get("experiment_id")))
        bump(cell_id, "evidence")
    for run in agent_runs or ():
        bump(getattr(run, "cell_id", None), "agent_runs")
    return dict(sorted(out.items()))
