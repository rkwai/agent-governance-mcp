"""Deterministic company clock: Maintain and Continue forces.

This process applies already-frozen stop rules, lease expiry, and work deadlines.
Maintain repairs leases and overdue open work. Continue closes due experiments
and creates independent evaluator closeout work. It does not invoke a model,
schedule agents, or change strategy.

Run it from the same hosted image; do not add a new Railway service.

  AI_NATURE_COMPANY_ID=<uuid> DATABASE_URL=<postgres> ai-nature-tick
  ai-nature-tick --company-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence

from .postgres import PostgresRepository
from .service import create_service


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply frozen stop rules, expire due leases, and fail overdue open work.",
    )
    parser.add_argument(
        "--company-id",
        help="Company to tick. Defaults to AI_NATURE_COMPANY_ID.",
    )
    return parser


async def run_tick(
    company_id: str, *, environ: Mapping[str, str] | None = None
) -> dict[str, object]:
    env = os.environ if environ is None else environ
    database_url = env.get("AI_NATURE_DATABASE_URL") or env.get("DATABASE_URL")
    if not database_url or not database_url.strip():
        raise RuntimeError(
            "AI_NATURE_DATABASE_URL or DATABASE_URL is required for the runtime tick"
        )
    store = PostgresRepository(database_url.strip())
    await store.open()
    try:
        service = create_service(store=store)
        return dict(await service.tick(company_id))
    finally:
        await store.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    company_id = args.company_id or os.environ.get("AI_NATURE_COMPANY_ID")
    if company_id is None or not str(company_id).strip():
        print("AI_NATURE_COMPANY_ID or --company-id is required", file=sys.stderr)
        return 2
    result = asyncio.run(run_tick(str(company_id).strip()))
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the console entry point
    raise SystemExit(main())
