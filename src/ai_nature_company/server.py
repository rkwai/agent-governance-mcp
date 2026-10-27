"""Official MCP SDK adapter for the deterministic company substrate.

This module is deliberately thin.  It owns protocol registration, authenticated
request context resolution, bounded JSON envelopes, and expected-error mapping.
All company behavior remains behind :class:`SubstrateService`.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic_core import to_jsonable_python

from . import __version__
from .attribution import ToolCallAuditEvent, ToolCallAuditSink, ToolCallPhase
from .auth import VerifiedPrincipal, is_verified_principal
from .errors import AuthorizationError, SubstrateError
from .forces import LEDGER_HANDOFF_AFTER_MUTATE

SERVER_NAME = "ai-nature-company"
LOGGER = logging.getLogger(__name__)
MAX_PAYLOAD_BYTES = 1_048_576
MAX_RESULT_BYTES = 1_048_576
MAX_ERROR_MESSAGE_CHARS = 500


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Stable metadata for one public Phase 1 operation."""

    name: str
    description: str
    read_only: bool = False


PHASE_ONE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "company.describe",
        "Describe authenticated identity and portfolio with an empty payload. "
        "Use cell_records {cell_id, kind, limit?, after_id?} to page work_item, offer, "
        "experiment, evidence or learning records in an authorized cell. This is the "
        "read-back after a ledger write; do not wait for a peer DM. "
        "Force-peer silence is not silence toward founder or other externals.",
        True,
    ),
    ToolSpec("company.bootstrap", "Bootstrap a company under explicit founder authority."),
    ToolSpec("constitution.get", "Read the active founder-controlled company constitution.", True),
    ToolSpec(
        "constitution.amend",
        "Activate the next founder-controlled constitution version and realign constraint ledgers.",
    ),
    ToolSpec(
        "protocol.describe",
        "Describe substrate capabilities, transition rules, natural-force physics, and "
        "the host-neutral bet workflow. After a ledger mutate, the next step is stop or "
        "read-back — not a peer DM or agent chat. Force-peer silence is distinct from "
        "external/host chat, which remains normal.",
        True,
    ),
    ToolSpec("agent.register", "Register an externally hosted agent principal and definition."),
    ToolSpec("agent.get", "Read one registered agent definition.", True),
    ToolSpec("agent.list", "List registered agents visible to the authenticated principal.", True),
    ToolSpec(
        "work.create",
        "Create a bounded work item for an offer cell." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "work.claim",
        "Claim a work item under a time-bounded lease." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "work.heartbeat",
        "Renew an active work lease." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "work.complete",
        "Complete leased work with an evidence-backed result." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec("work.fail", "Record a leased work attempt as failed." + LEDGER_HANDOFF_AFTER_MUTATE),
    ToolSpec(
        "work.release",
        "Release a work lease for another eligible agent." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "work.cancel",
        "Owner-signed cancel of never-claimed work on the caller's own cell. "
        "Requires a non-empty reason. Refuses claimed or leased work and other cells. "
        "Moves the item to cancelled so it leaves stale_open_work_count."
        + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec("cell.create", "Create an isolated offer cell." + LEDGER_HANDOFF_AFTER_MUTATE),
    ToolSpec("cell.get", "Read one offer cell and its current projection.", True),
    ToolSpec("cell.list", "List offer cells visible to the authenticated principal.", True),
    ToolSpec(
        "cell.transition",
        "Apply an authorized offer-cell state transition. Kill is founder-signed, or "
        "owner-signed with a required founder_decision_ref on a cell with no open "
        "experiments and no open or claimed work; optional reason. Both persist on "
        "the audit trail. Kill ends every running agent run of the cell in the same "
        "transaction and lists ended_agent_run_ids." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "maintenance.run",
        "On-demand Maintain/Continue scan (nothing is scheduled). Founder: company-wide "
        "or one cell_id; cell owner: own cell_id only. Returns findings and would-do. "
        "With end_runs_on_killed_cells true it ends agent runs still running on killed "
        "cells in scope; the audit row lists ended_agent_run_ids." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "cell.amend",
        "Owner-signed revise of a cell's objective text. Requires founder_decision_ref. "
        "Own active non-terminal cell only; cannot change budget, stage, owner, "
        "authority, or constitution." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "offer.register",
        "Register the first version of an offer in a cell." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "offer.mutate",
        "Create a traceable mutation of an existing offer." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "experiment.register",
        "Register an experiment and its success criteria." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "experiment.start",
        "Start a registered experiment within approved constraints." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "experiment.close",
        "Close a running experiment into evaluation. Optional reason and "
        "founder_decision_ref persist on the experiment and audit trail."
        + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "experiment.amend",
        "Owner-signed amend of a running experiment in the caller's own cell. "
        "May extend the window, revise stop rules, treatment, or hypothesis "
        "window text, change budget_amount_minor through the reservation ledger, "
        "or set max_external_messages. Requires founder_decision_ref. Cannot "
        "retrofit metrics, thresholds, budget unit, evaluator, or cell, or amend "
        "the constitution." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "experiment.record_send",
        "Owner-signed record of one founder-signed external send. Debits exactly "
        "one try. Idempotent per send_id; refuses double counting. Enforces the "
        "experiment max_external_messages override when present, otherwise the "
        "stored constitution max_external_messages_per_experiment; refusal "
        "names remaining sends. Requires founder_signoff_ref and evidence_id. "
        "sent_at is the founder/Gmail-verified send time and is not "
        "cross-checked against "
        "evidence timestamps. Running experiment only." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "evidence.append",
        "Append a provenance-bearing evidence envelope." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "economics.append",
        "Append verified economic observations for an offer cell." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "evaluation.submit",
        "Submit an independent evaluation of a closed experiment. Must cite "
        "window_ends_at. A count criterion observed 0 with no qualifying "
        "fitness evidence may fail with empty evidence_ids; passed still "
        "requires evidence." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "gate.evaluate",
        "Evaluate deterministic promotion and safety gates." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "allocation.propose",
        "Propose a bounded resource allocation change." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "allocation.commit",
        "Commit an authorized allocation proposal." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec(
        "learning.propose",
        "Propose a reusable learning from verified evidence." + LEDGER_HANDOFF_AFTER_MUTATE,
    ),
    ToolSpec("learning.promote", "Promote an approved learning into active company memory."),
    ToolSpec(
        "audit.list",
        "List bounded audit records visible to the authenticated principal. "
        "Cell-owner bindings see only their bound cell; events without a "
        "provable cell_id are omitted. Work events are included when the work "
        "item belongs to that cell.",
        True,
    ),
    ToolSpec(
        "decision.explain",
        "Explain a recorded deterministic decision from its inputs.",
        True,
    ),
)

PHASE_ONE_TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in PHASE_ONE_TOOLS)


class SubstrateService(Protocol):
    """Business boundary consumed by the MCP adapter.

    Implementations are asynchronous so persistence can retain transaction scope
    without blocking the MCP event loop.
    """

    async def invoke(
        self,
        tool_name: str,
        auth_context: VerifiedPrincipal,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Execute one authorized deterministic substrate operation."""


type MaybeAuthority = VerifiedPrincipal | Awaitable[VerifiedPrincipal]


class AuthResolver(Protocol):
    """Resolve verified authority from server-owned request context."""

    def resolve(self, context: Context[Any, Any]) -> MaybeAuthority:
        """Return a verified principal; never inspect tool payload authority fields."""


@dataclass(frozen=True, slots=True)
class StaticAuthResolver:
    """Resolver for stdio where trusted startup configuration fixes the principal."""

    authority: VerifiedPrincipal

    def __post_init__(self) -> None:
        if not is_verified_principal(self.authority):
            raise TypeError("static authority must be a VerifiedPrincipal")

    def resolve(self, context: Context[Any, Any]) -> VerifiedPrincipal:
        del context
        return self.authority


type TokenAuthenticator = Callable[[AccessToken], MaybeAuthority]
type ServerLifespan = Callable[[MCPServer[Any]], AbstractAsyncContextManager[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class SDKAccessTokenResolver:
    """Resolve HTTP authority from an SDK-verified bearer access token.

    ``authenticate`` maps the token object produced by the configured MCP token
    verifier to the application's immutable verified-principal representation.
    Raw request headers and tool arguments are intentionally never consulted.
    """

    authenticate: TokenAuthenticator

    async def resolve(self, context: Context[Any, Any]) -> VerifiedPrincipal:
        del context
        token = get_access_token()
        if token is None:
            raise AuthorizationError("authenticated access token required")
        resolved = self.authenticate(token)
        if inspect.isawaitable(resolved):
            resolved = await resolved
        if not is_verified_principal(resolved):
            raise AuthorizationError("access token did not resolve to a principal")
        return resolved


def _encoded_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _expected_error(error: SubstrateError) -> ToolError:
    # Domain errors are caller-visible, but third-party validators can attach
    # rejected values on later lines.  Keep only the bounded summary line so
    # payload content cannot be copied into SDK logs or tool output.
    message = str(error).strip().splitlines()[0][:MAX_ERROR_MESSAGE_CHARS]
    payload: dict[str, object] = {
        "code": error.code,
        "message": message,
    }
    details = getattr(error, "details", None)
    if isinstance(details, Mapping) and details:
        for key, value in details.items():
            if isinstance(key, str) and isinstance(value, str) and key not in payload:
                payload[key] = value
    envelope = {"error": payload}
    return ToolError(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))


def _transport_error(code: str, message: str) -> ToolError:
    envelope = {"error": {"code": code, "message": message}}
    return ToolError(json.dumps(envelope, separators=(",", ":")))


async def _resolve_authority(
    resolver: AuthResolver,
    context: Context[Any, Any],
) -> VerifiedPrincipal:
    authority = resolver.resolve(context)
    if inspect.isawaitable(authority):
        authority = await authority
    if not is_verified_principal(authority):
        raise AuthorizationError("request did not resolve to a principal")
    return authority


async def _record_call_event(
    sink: ToolCallAuditSink | None,
    event: ToolCallAuditEvent,
) -> None:
    if sink is None:
        return
    result = sink.record(event)
    if inspect.isawaitable(result):
        await result


def _make_handler(
    spec: ToolSpec,
    service: SubstrateService,
    auth_resolver: AuthResolver,
    call_audit_sink: ToolCallAuditSink | None,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def handler(
        context: Context[Any, Any],
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_payload = dict(payload or {})
        try:
            authority = await _resolve_authority(auth_resolver, context)
        except SubstrateError as error:
            raise _expected_error(error) from error

        call_id = str(uuid4())
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        try:
            request_id = str(context.request_id)
        except Exception:  # pragma: no cover - defensive SDK compatibility
            request_id = call_id
        try:
            protocol_version = str(context.protocol_version)
        except Exception:  # pragma: no cover - defensive SDK compatibility
            protocol_version = "unknown"
        received = ToolCallAuditEvent.received(
            call_id=call_id,
            request_id=request_id,
            protocol_version=protocol_version,
            tool_name=spec.name,
            principal=authority,
            payload=request_payload,
            occurred_at=started_at,
        )
        await _record_call_event(call_audit_sink, received)

        def elapsed_ms() -> int:
            return max(0, int((time.perf_counter() - started) * 1000))

        try:
            if _encoded_size(request_payload) > MAX_PAYLOAD_BYTES:
                raise _transport_error(
                    "payload_too_large",
                    "payload exceeds the MCP adapter limit",
                )
            result = await service.invoke(spec.name, authority, request_payload)
            if not isinstance(result, Mapping):
                raise TypeError("substrate service must return a mapping")
            response = to_jsonable_python(dict(result))
            if not isinstance(response, dict):  # pragma: no cover - guarded by mapping input
                raise TypeError("serialized substrate result must be an object")
            if _encoded_size(response) > MAX_RESULT_BYTES:
                raise _transport_error(
                    "result_too_large",
                    "result exceeds the MCP adapter limit",
                )
        except SubstrateError as error:
            phase = (
                ToolCallPhase.DENIED
                if isinstance(error, AuthorizationError)
                else ToolCallPhase.FAILED
            )
            await _record_call_event(
                call_audit_sink,
                received.terminal(
                    phase,
                    occurred_at=datetime.now(UTC),
                    duration_ms=elapsed_ms(),
                    error_code=error.code,
                ),
            )
            raise _expected_error(error) from error
        except ToolError:
            await _record_call_event(
                call_audit_sink,
                received.terminal(
                    ToolCallPhase.FAILED,
                    occurred_at=datetime.now(UTC),
                    duration_ms=elapsed_ms(),
                    error_code="transport_error",
                ),
            )
            raise
        except Exception:
            LOGGER.exception("tool %s raised an unexpected exception", spec.name)
            await _record_call_event(
                call_audit_sink,
                received.terminal(
                    ToolCallPhase.FAILED,
                    occurred_at=datetime.now(UTC),
                    duration_ms=elapsed_ms(),
                    error_code="internal_error",
                ),
            )
            raise _transport_error("internal_error", "internal tool error") from None

        # Keep the success-terminal write outside the dispatch try/except.  A sink
        # failure after the business operation has completed must fail the request,
        # but must never be reclassified as an internal execution failure or cause a
        # second, contradictory terminal event to be attempted.  The caller can
        # safely retry mutations through the substrate's idempotency contract.
        await _record_call_event(
            call_audit_sink,
            received.terminal(
                ToolCallPhase.SUCCEEDED,
                occurred_at=datetime.now(UTC),
                duration_ms=elapsed_ms(),
                response=response,
            ),
        )
        return response

    handler.__name__ = spec.name.replace(".", "_")
    handler.__qualname__ = handler.__name__
    handler.__doc__ = spec.description
    return handler


def build_server(
    service: SubstrateService,
    auth_resolver: AuthResolver,
    *,
    lifespan: ServerLifespan | None = None,
    auth: AuthSettings | None = None,
    token_verifier: TokenVerifier | None = None,
    call_audit_sink: ToolCallAuditSink | None = None,
) -> MCPServer:
    """Build the Phase 1 MCP server around explicit business and auth dependencies."""

    if service is None:
        raise TypeError("service is required")
    if auth_resolver is None:
        raise TypeError("auth_resolver is required")
    if isinstance(auth_resolver, SDKAccessTokenResolver) and (
        auth is None or token_verifier is None
    ):
        raise ValueError("SDKAccessTokenResolver requires MCP AuthSettings and a TokenVerifier")

    server = MCPServer(
        SERVER_NAME,
        title="AI Nature Company",
        description="Deterministic substrate for an agent-operated evolutionary company.",
        instructions=(
            "Start with company.describe using an empty payload and verify its server-resolved "
            "identity against the operator inventory. Stop on mismatch. Then read "
            "constitution.get and protocol.describe for the current bet workflow. "
            "Use these tools to operate offer cells through explicit work, evidence, "
            "evaluation, policy, and allocation records. After a mutate that advances "
            "cell, run, evidence, lease, or tries, stop or read back via company.describe "
            "/ cell_records. Shared MCP state is the force handoff; do not DM a peer or "
            "use agent chat for routine Sense, Act, or Bound handoffs. Channel membership "
            "is not a work queue. Force-peer silence is not silence toward founder, "
            "humans, or other external entities; acknowledge and reply when they are "
            "waiting. Model inference and agent scheduling remain outside this "
            "server. Discovery and work leases do not grant external-action approval."
        ),
        version=__version__,
        lifespan=lifespan,
        auth=auth,
        token_verifier=token_verifier,
    )

    for spec in PHASE_ONE_TOOLS:
        server.add_tool(
            _make_handler(spec, service, auth_resolver, call_audit_sink),
            name=spec.name,
            description=spec.description,
            annotations=ToolAnnotations(
                readOnlyHint=spec.read_only,
                destructiveHint=False,
                idempotentHint=True,
                openWorldHint=False,
            ),
            structured_output=True,
        )

    return server


__all__ = [
    "MAX_ERROR_MESSAGE_CHARS",
    "MAX_PAYLOAD_BYTES",
    "MAX_RESULT_BYTES",
    "PHASE_ONE_TOOLS",
    "PHASE_ONE_TOOL_NAMES",
    "SERVER_NAME",
    "AuthResolver",
    "SDKAccessTokenResolver",
    "ServerLifespan",
    "StaticAuthResolver",
    "SubstrateService",
    "TokenAuthenticator",
    "ToolCallAuditSink",
    "ToolSpec",
    "build_server",
]
