#!/usr/bin/env python3
"""Read-only MCP call-cardinality and connector-to-identity map audit."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from scripts.operator_common import (
    Connection,
    OperatorError,
    canonical_uuid,
    connect_operator,
    database_url_from_environment,
    require_operator_role,
)


def audit_call_cardinality(connection: Connection, *, company_id: str) -> Mapping[str, object]:
    """Detect received rows without exactly one terminal event."""

    require_operator_role(connection)
    connection.execute(
        "SELECT set_config('app.company_id', %(company_id)s, true)",
        {"company_id": company_id},
    )
    violations = connection.execute(
        """
        SELECT received.call_id,
               received.auth_subject_id,
               received.principal_id,
               received.agent_instance_id,
               received.agent_run_id,
               COUNT(terminal.call_id) AS terminal_count
        FROM public.anc_mcp_tool_calls AS received
        LEFT JOIN public.anc_mcp_tool_calls AS terminal
          ON terminal.company_id = received.company_id
         AND terminal.call_id = received.call_id
         AND terminal.phase IN ('succeeded', 'denied', 'failed')
        WHERE received.company_id = %(company_id)s
          AND received.phase = 'received'
        GROUP BY received.call_id, received.auth_subject_id, received.principal_id,
                 received.agent_instance_id, received.agent_run_id
        HAVING COUNT(terminal.call_id) <> 1
        ORDER BY received.call_id
        """,
        {"company_id": company_id},
    ).fetchall()
    identity_map = connection.execute(
        """
        SELECT subject.oauth_client_id,
               subject.auth_subject_id,
               subject.principal_id,
               instance.agent_instance_id,
               instance.role,
               instance.cell_id,
               run.agent_run_id,
               run.status
        FROM public.anc_auth_subjects AS subject
        LEFT JOIN public.anc_agent_instances AS instance
          ON instance.company_id = subject.company_id
         AND instance.principal_id = subject.principal_id
        LEFT JOIN public.anc_agent_runs AS run
          ON run.company_id = instance.company_id
         AND run.agent_instance_id = instance.agent_instance_id
         AND run.status = 'running'
        WHERE subject.company_id = %(company_id)s
        ORDER BY subject.oauth_client_id, subject.auth_subject_id
        """,
        {"company_id": company_id},
    ).fetchall()
    if violations:
        raise OperatorError(
            f"{len(violations)} received call(s) do not have exactly one terminal row"
        )
    return MappingProxyType(
        {
            "status": "pass",
            "company_id": company_id,
            "read_only": True,
            "received_without_one_terminal": 0,
            "connector_identity_map": [
                {
                    "oauth_client_id": row[0],
                    "auth_subject_id": row[1],
                    "principal_id": row[2],
                    "agent_instance_id": row[3],
                    "role": row[4],
                    "cell_id": row[5],
                    "running_agent_run_id": row[6],
                    "running_run_status": row[7],
                }
                for row in identity_map
            ],
        }
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only audit of MCP received/terminal cardinality.",
    )
    parser.add_argument("command", choices=("audit",))
    parser.add_argument("--company-id", required=True, help="canonical company UUID")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        company_id = canonical_uuid(args.company_id, "company_id")
        dsn = database_url_from_environment()
        try:
            with connect_operator(dsn) as connection:
                report = audit_call_cardinality(connection, company_id=company_id)
        except Exception as error:
            if isinstance(error, OperatorError):
                raise
            raise OperatorError("call-cardinality audit failed") from error
        print(json.dumps(dict(report), indent=2, sort_keys=True))
        return 0
    except OperatorError as error:
        print(f"audit failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
