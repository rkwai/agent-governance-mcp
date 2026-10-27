#!/usr/bin/env python3
"""List running agent runs that expire within 72 hours.

This is an operator inspection command.  It uses the existing operator SELECT
on ``anc_agent_runs`` and does not grant or require EXECUTE on the runtime
resolver.  Forced RLS only exposes the scoped company, so the command requires
``--company-id`` and sets ``app.company_id`` inside an explicit transaction.
Identity-resolution smoke against Postgres is the CI test
``test_supabase_public_client_id_and_session_subject_resolver_contract``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from scripts.operator_common import (
    Connection,
    OperatorError,
    canonical_uuid,
    connect_operator,
    database_url_from_environment,
    require_operator_role,
    timestamp_text,
)

LIST_EXPIRING_RUNS = """
SELECT agent_run_id, agent_instance_id, expires_at
FROM public.anc_agent_runs
WHERE status = 'running'
  AND started_at <= now()
  AND expires_at > now()
  AND expires_at <= now() + interval '72 hours'
ORDER BY expires_at ASC, agent_run_id ASC
LIMIT 50
"""


def list_expiring_agent_runs_report(
    rows: Sequence[Sequence[Any]],
    *,
    company_id: str,
    observed_at: datetime,
) -> dict[str, object]:
    runs = []
    for row in rows:
        agent_run_id, agent_instance_id, expires_at = row
        runs.append(
            {
                "agent_run_id": str(agent_run_id),
                "agent_instance_id": str(agent_instance_id),
                "expires_at": timestamp_text(expires_at),
            }
        )
    return {
        "status": "ok",
        "company_id": company_id,
        "window": "72h",
        "count": len(runs),
        "runs": runs,
        "observed_at": timestamp_text(observed_at),
    }


def list_expiring_agent_runs(
    connection: Connection,
    *,
    company_id: str,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    """Return expiring runs visible under the caller's company RLS scope."""

    observed = datetime.now(UTC) if observed_at is None else observed_at
    with connection.transaction():
        require_operator_role(connection)
        connection.execute(
            "SELECT set_config('app.company_id', %(company_id)s, true)",
            {"company_id": company_id},
        )
        rows = connection.execute(LIST_EXPIRING_RUNS).fetchall()
        return list_expiring_agent_runs_report(
            rows,
            company_id=company_id,
            observed_at=observed,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List running agent runs that expire within 72 hours.",
    )
    parser.add_argument("--company-id", required=True, help="canonical company UUID")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        company_id = canonical_uuid(args.company_id, "company_id")
        dsn = database_url_from_environment()
        try:
            with connect_operator(dsn) as connection:
                report = list_expiring_agent_runs(connection, company_id=company_id)
        except Exception as error:
            if isinstance(error, OperatorError):
                raise
            raise OperatorError("could not list expiring agent runs") from error
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except OperatorError as error:
        print(f"list-expiring failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
