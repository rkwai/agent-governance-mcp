"""Content-minimized, append-only attribution for authenticated MCP tool calls.

The transport records one ``received`` event before dispatch and one terminal event
after dispatch.  Arguments and results are represented only by canonical SHA-256
digests.  Raw bearer tokens, prompts, payloads, results, and hidden reasoning never
enter this record.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic_core import to_jsonable_python

from .auth import VerifiedPrincipal, is_verified_principal


class ToolCallPhase(StrEnum):
    """Append-only lifecycle events for one authenticated tool invocation."""

    RECEIVED = "received"
    SUCCEEDED = "succeeded"
    DENIED = "denied"
    FAILED = "failed"


def canonical_sha256(value: object) -> str:
    """Hash a JSON-compatible value without retaining its content."""

    normalized = to_jsonable_python(value)
    encoded = json.dumps(
        normalized,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolCallAuditEvent:
    """One immutable, server-derived call-attribution lifecycle event."""

    call_id: str
    phase: ToolCallPhase
    request_id: str
    protocol_version: str
    company_id: str
    principal_id: str
    authentication_method: str
    authentication_id: str
    tool_name: str
    arguments_sha256: str
    occurred_at: datetime
    auth_subject_id: str | None = None
    agent_instance_id: str | None = None
    definition_id: str | None = None
    definition_version: int | None = None
    role: str | None = None
    cell_id: str | None = None
    agent_run_id: str | None = None
    correlation_id: str | None = None
    idempotency_key_sha256: str | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    response_sha256: str | None = None

    @classmethod
    def received(
        cls,
        *,
        call_id: str,
        request_id: str,
        protocol_version: str,
        tool_name: str,
        principal: VerifiedPrincipal,
        payload: Mapping[str, Any],
        occurred_at: datetime,
    ) -> ToolCallAuditEvent:
        """Create the mandatory pre-dispatch event from verified context."""

        if not is_verified_principal(principal):
            raise TypeError("principal must be verified")
        identity = principal.agent_identity
        role = identity.role.value if identity is not None else None
        if role is None and len(principal.roles) == 1:
            role = next(iter(principal.roles)).value
        correlation = payload.get("correlation_id")
        correlation_id = correlation if isinstance(correlation, str) and correlation else None
        idempotency_key = payload.get("idempotency_key")
        return cls(
            call_id=call_id,
            phase=ToolCallPhase.RECEIVED,
            request_id=request_id,
            protocol_version=protocol_version,
            company_id=principal.company_id,
            principal_id=principal.principal_id,
            authentication_method=principal.authentication_method,
            authentication_id=principal.authentication_id,
            auth_subject_id=principal.auth_subject_id,
            agent_instance_id=identity.agent_instance_id if identity is not None else None,
            definition_id=identity.definition_id if identity is not None else None,
            definition_version=identity.definition_version if identity is not None else None,
            role=role,
            cell_id=identity.cell_id if identity is not None else None,
            agent_run_id=principal.agent_run_id,
            tool_name=tool_name,
            arguments_sha256=canonical_sha256(dict(payload)),
            correlation_id=correlation_id,
            idempotency_key_sha256=(
                canonical_sha256(idempotency_key)
                if isinstance(idempotency_key, str) and idempotency_key
                else None
            ),
            occurred_at=occurred_at.astimezone(UTC),
        )

    def terminal(
        self,
        phase: ToolCallPhase,
        *,
        occurred_at: datetime,
        duration_ms: int,
        error_code: str | None = None,
        response: object | None = None,
    ) -> ToolCallAuditEvent:
        """Create exactly one terminal event without mutating the received event."""

        if self.phase is not ToolCallPhase.RECEIVED:
            raise ValueError("terminal events must derive from a received event")
        if phase not in {
            ToolCallPhase.SUCCEEDED,
            ToolCallPhase.DENIED,
            ToolCallPhase.FAILED,
        }:
            raise ValueError("terminal phase is required")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
            raise ValueError("duration_ms must be a non-negative integer")
        return replace(
            self,
            phase=phase,
            occurred_at=occurred_at.astimezone(UTC),
            duration_ms=duration_ms,
            error_code=error_code,
            response_sha256=(canonical_sha256(response) if response is not None else None),
        )


@runtime_checkable
class ToolCallAuditSink(Protocol):
    """Durably append one call lifecycle event."""

    def record(self, event: ToolCallAuditEvent) -> Awaitable[None] | None:
        """Persist the event without modifying or enriching trusted identity."""


type ToolCallAuditRecorder = Callable[[ToolCallAuditEvent], Awaitable[None] | None]


__all__ = [
    "ToolCallAuditEvent",
    "ToolCallAuditRecorder",
    "ToolCallAuditSink",
    "ToolCallPhase",
    "canonical_sha256",
]
