from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_nature_company.auth import (
    AgentIdentityContext,
    LocalTestAuthenticator,
    PrincipalRecord,
    ResourceScope,
    Role,
    ScopedRoleGrant,
)
from ai_nature_company.postgres import PostgresRepository
from ai_nature_company.service import create_service
from scripts.bootstrap_closed_beta import (
    OPERATOR_DATABASE_ROLE,
    BootstrapManifest,
    apply_bootstrap,
)
from scripts.provision_agent_binding import (
    ProvisioningError,
    ProvisioningSpec,
    apply_provisioning,
    apply_rotation,
)
from tests.operations.test_bootstrap_closed_beta import (
    CELL_BUDGET_ID,
    CELL_ID,
    COMPANY_BUDGET_ID,
    COMPANY_ID,
    CONSTITUTION_ID,
    FOUNDER_ID,
    OWNER_INSTANCE_ID,
    OWNER_PRINCIPAL_ID,
    SPECIALIST_INSTANCE_ID,
    SPECIALIST_PRINCIPAL_ID,
)
from tests.operations.test_bootstrap_closed_beta import (
    manifest as bootstrap_manifest,
)

DATABASE_URL = os.getenv("ANC_TEST_DATABASE_URL")
CLIENT_ID = "00000000-0000-4000-8000-00000000000b"
SUBJECT_ID = "00000000-0000-4000-8000-00000000000c"
RUN_ID = "00000000-0000-4000-8000-00000000000d"
SUCCESSOR_RUN_ID = "00000000-0000-4000-8000-00000000000e"
RACING_RUN_ID = "00000000-0000-4000-8000-00000000000f"
ROTATION_COMPANY_ID = "00000000-0000-4000-8000-000000000101"
ROTATION_CELL_ID = "00000000-0000-4000-8000-000000000105"
ROTATION_SPECIALIST_PRINCIPAL_ID = "00000000-0000-4000-8000-000000000109"
ROTATION_SPECIALIST_INSTANCE_ID = "00000000-0000-4000-8000-00000000010a"
ROTATION_CLIENT_ID = "00000000-0000-4000-8000-00000000010b"
ROTATION_SUBJECT_ID = "00000000-0000-4000-8000-00000000010c"
ROTATION_RUN_ID = "00000000-0000-4000-8000-00000000010d"
ROTATION_SUCCESSOR_RUN_ID = "00000000-0000-4000-8000-00000000010e"
ROTATION_RACING_RUN_ID = "00000000-0000-4000-8000-00000000010f"

_TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
_EXPECTED_OPERATOR_TABLE_PRIVILEGES = {
    "anc_domain_events": frozenset({"SELECT", "INSERT"}),
    "anc_audit_events": frozenset({"SELECT", "INSERT"}),
    "anc_stream_heads": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "anc_projections": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "anc_idempotency_records": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "anc_budgets": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "anc_budget_reservations": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "anc_work_leases": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "anc_oauth_clients": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "anc_oauth_client_resource_aliases": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "anc_auth_subjects": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "anc_agent_instances": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "anc_agent_runs": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "anc_mcp_tool_calls": frozenset({"SELECT"}),
}
_SEQUENCE_PRIVILEGES = ("SELECT", "USAGE", "UPDATE")
_EXPECTED_OPERATOR_SEQUENCE_PRIVILEGES = {
    "anc_domain_events_position_seq": frozenset({"SELECT", "USAGE"}),
    "anc_audit_events_position_seq": frozenset({"SELECT", "USAGE"}),
    "anc_mcp_tool_calls_position_seq": frozenset(),
}

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="set ANC_TEST_DATABASE_URL to run the closed-beta PostgreSQL E2E",
)


async def _configure_operator(connection: object) -> None:
    await connection.execute(f"SET ROLE {OPERATOR_DATABASE_ROLE}")
    await connection.execute("SET row_security = on")
    await connection.commit()


async def _assert_operator_role_contract(connection: object) -> None:
    role_cursor = await connection.execute(
        """
        SELECT role.rolcanlogin, role.rolsuper, role.rolbypassrls,
               role.rolcreatedb, role.rolcreaterole, role.rolreplication,
               role.rolinherit,
               EXISTS (
                   SELECT 1
                   FROM pg_catalog.pg_auth_members AS membership
                   WHERE membership.member = role.oid
               ) AS has_role_membership
        FROM pg_catalog.pg_roles AS role
        WHERE role.rolname = %(role)s
        """,
        {"role": OPERATOR_DATABASE_ROLE},
    )
    assert await role_cursor.fetchone() == (
        True,
        False,
        False,
        False,
        False,
        False,
        False,
        False,
    )

    table_cursor = await connection.execute(
        """
        SELECT relation_name, privilege,
               has_table_privilege(
                   %(role)s,
                   format('public.%%I', relation_name),
                   privilege
               )
        FROM unnest(%(relations)s::text[]) AS relation(relation_name)
        CROSS JOIN unnest(%(privileges)s::text[]) AS action(privilege)
        ORDER BY relation_name, privilege
        """,
        {
            "role": OPERATOR_DATABASE_ROLE,
            "relations": list(_EXPECTED_OPERATOR_TABLE_PRIVILEGES),
            "privileges": list(_TABLE_PRIVILEGES),
        },
    )
    effective_table_privileges: dict[str, set[str]] = {
        relation: set() for relation in _EXPECTED_OPERATOR_TABLE_PRIVILEGES
    }
    for relation, privilege, granted in await table_cursor.fetchall():
        if granted:
            effective_table_privileges[relation].add(privilege)
    assert effective_table_privileges == {
        relation: set(privileges)
        for relation, privileges in _EXPECTED_OPERATOR_TABLE_PRIVILEGES.items()
    }

    sequence_cursor = await connection.execute(
        """
        SELECT sequence_name, privilege,
               has_sequence_privilege(
                   %(role)s,
                   format('public.%%I', sequence_name),
                   privilege
               )
        FROM unnest(%(sequences)s::text[]) AS sequence(sequence_name)
        CROSS JOIN unnest(%(privileges)s::text[]) AS action(privilege)
        ORDER BY sequence_name, privilege
        """,
        {
            "role": OPERATOR_DATABASE_ROLE,
            "sequences": list(_EXPECTED_OPERATOR_SEQUENCE_PRIVILEGES),
            "privileges": list(_SEQUENCE_PRIVILEGES),
        },
    )
    effective_sequence_privileges: dict[str, set[str]] = {
        sequence: set() for sequence in _EXPECTED_OPERATOR_SEQUENCE_PRIVILEGES
    }
    for sequence, privilege, granted in await sequence_cursor.fetchall():
        if granted:
            effective_sequence_privileges[sequence].add(privilege)
    assert effective_sequence_privileges == {
        sequence: set(privileges)
        for sequence, privileges in _EXPECTED_OPERATOR_SEQUENCE_PRIVILEGES.items()
    }

    function_cursor = await connection.execute(
        """
        SELECT
            has_function_privilege(
                %(role)s,
                'public.anc_resolve_agent_identity(text,text,text,text,text)',
                'EXECUTE'
            ),
            has_function_privilege(
                %(role)s,
                'public.anc_auth_catalog_compatible()',
                'EXECUTE'
            ),
            has_function_privilege(
                %(role)s,
                'public.anc_inspect_agent_run_binding(text,text,text,text,text)',
                'EXECUTE'
            ),
            has_function_privilege(
                %(role)s,
                'public.anc_list_expiring_agent_runs()',
                'EXECUTE'
            )
        """,
        {"role": OPERATOR_DATABASE_ROLE},
    )
    assert await function_cursor.fetchone() == (False, False, False, False)


def _rotation_bootstrap_mapping() -> dict[str, object]:
    replacements = {
        COMPANY_ID: ROTATION_COMPANY_ID,
        FOUNDER_ID: "00000000-0000-4000-8000-000000000102",
        CONSTITUTION_ID: "00000000-0000-4000-8000-000000000103",
        COMPANY_BUDGET_ID: "00000000-0000-4000-8000-000000000104",
        CELL_ID: ROTATION_CELL_ID,
        CELL_BUDGET_ID: "00000000-0000-4000-8000-000000000106",
        OWNER_PRINCIPAL_ID: "00000000-0000-4000-8000-000000000107",
        OWNER_INSTANCE_ID: "00000000-0000-4000-8000-000000000108",
        SPECIALIST_PRINCIPAL_ID: ROTATION_SPECIALIST_PRINCIPAL_ID,
        SPECIALIST_INSTANCE_ID: ROTATION_SPECIALIST_INSTANCE_ID,
    }
    serialized = json.dumps(bootstrap_manifest())
    for source, target in replacements.items():
        serialized = serialized.replace(source, target)
    return json.loads(serialized)


def _identity_spec(
    manifest: BootstrapManifest,
    *,
    company_id: str = COMPANY_ID,
    cell_id: str = CELL_ID,
    principal_id: str = SPECIALIST_PRINCIPAL_ID,
    instance_id: str = SPECIALIST_INSTANCE_ID,
    oauth_client_id: str = CLIENT_ID,
    external_subject: str = SUBJECT_ID,
    agent_run_id: str = RUN_ID,
    rotate: bool = False,
    run_created_at: str | None = None,
    run_started_at: str = "2026-08-30T00:00:00Z",
    run_expires_at: str = "2099-01-01T00:00:00Z",
    rotation_kind: str | None = None,
    now: datetime | None = None,
    resource_url: str = "https://mcp.e2e.example.test/mcp",
) -> ProvisioningSpec:
    agent = next(agent for agent in manifest.agents if agent.role is Role.SPECIALIST)
    payload: dict[str, object] = {
        "company_id": company_id,
        "oauth_client_id": oauth_client_id,
        "oauth_client_display_name": "PostgreSQL E2E specialist connector",
        "resource_url": resource_url,
        "issuer": "https://project-ref.supabase.co/auth/v1",
        "external_subject": external_subject,
        "principal_id": principal_id,
        "agent_instance_id": instance_id,
        "definition_id": agent.definition_id,
        "definition_version": agent.definition_version,
        "role": agent.role.value,
        "cell_id": cell_id,
        "agent_run_id": agent_run_id,
        "provisioned_at": "2026-08-30T00:00:00Z",
        "run_started_at": run_started_at,
        "run_expires_at": run_expires_at,
    }
    if rotate:
        assert run_created_at is not None
        assert rotation_kind is not None
        payload["run_created_at"] = run_created_at
        payload["rotation_kind"] = rotation_kind
    return ProvisioningSpec.from_mapping(
        payload,
        definition=agent.definition,
        now=now or datetime(2026, 8, 30, 1, tzinfo=UTC),
        rotate=rotate,
    )


def _operator_connection(psycopg: object, *, company_id: str = COMPANY_ID):
    connection = psycopg.connect(DATABASE_URL, autocommit=True)
    connection.execute(f"SET ROLE {OPERATOR_DATABASE_ROLE}")
    connection.execute("SET row_security = on")
    connection.execute(
        "SELECT set_config('app.company_id', %s, false)",
        (company_id,),
    )
    return connection


async def _prepare_closed_beta_identity(
    psycopg: object,
    psycopg_pool: object,
    *,
    mapping: dict[str, object] | None = None,
    identity_kwargs: dict[str, object] | None = None,
):
    assert DATABASE_URL is not None
    async with await psycopg.AsyncConnection.connect(
        DATABASE_URL,
        autocommit=True,
    ) as admin:
        for migration in sorted(Path("supabase/migrations").glob("*.sql")):
            await admin.execute(migration.read_text(encoding="utf-8"))
        await _assert_operator_role_contract(admin)

    pool = psycopg_pool.AsyncConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=2,
        open=False,
        configure=_configure_operator,
    )
    desired_bootstrap = BootstrapManifest.from_mapping(mapping or bootstrap_manifest())
    identity = _identity_spec(desired_bootstrap, **(identity_kwargs or {}))
    return pool, desired_bootstrap, identity


async def test_fresh_database_bootstrap_identity_activation_and_describe_readiness() -> None:
    psycopg = pytest.importorskip("psycopg")
    psycopg_pool = pytest.importorskip("psycopg_pool")
    assert DATABASE_URL is not None

    pool, desired_bootstrap, identity = await _prepare_closed_beta_identity(psycopg, psycopg_pool)

    async with pool:
        repository = PostgresRepository(pool=pool)
        bootstrap_result = await apply_bootstrap(desired_bootstrap, repository)
        assert bootstrap_result["identity_activation"] == "ready-not-performed"

        # The sync operator connection exercises the same interface used by the
        # one-shot identity CLI, after canonical domain projections are durable.
        with psycopg.connect(DATABASE_URL, autocommit=True) as operator:
            operator.execute(f"SET ROLE {OPERATOR_DATABASE_ROLE}")
            operator.execute("SET row_security = on")
            provisioned = apply_provisioning(identity, operator)
            assert provisioned["agent_instance_id"] == SPECIALIST_INSTANCE_ID
            alias_identity = _identity_spec(
                desired_bootstrap,
                resource_url="https://mcp-alias.e2e.example.test/mcp",
            )
            aliased = apply_provisioning(alias_identity, operator)
            assert aliased["principal_id"] == SPECIALIST_PRINCIPAL_ID
            assert aliased["agent_run_id"] == RUN_ID

            with operator.transaction():
                operator.execute(
                    "SELECT set_config('app.company_id', %(company_id)s, true)",
                    {"company_id": COMPANY_ID},
                )
                listed = operator.execute(
                    """
                    SELECT resource_url
                    FROM public.anc_oauth_client_resource_aliases
                    WHERE company_id = %(company_id)s
                      AND client_id = %(oauth_client_id)s
                    ORDER BY resource_url
                    """,
                    identity.parameters,
                ).fetchall()
                assert [item[0] for item in listed] == [
                    "https://mcp-alias.e2e.example.test/mcp",
                    "https://mcp.e2e.example.test/mcp",
                ]
                row = operator.execute(
                    """
                    SELECT subject.auth_subject_id, subject.principal_id,
                           instance.agent_instance_id, instance.definition_id,
                           instance.definition_version, instance.role, instance.cell_id,
                           run.agent_run_id, run.status, run.expires_at,
                           subject.token_valid_after
                    FROM public.anc_auth_subjects AS subject
                    JOIN public.anc_agent_instances AS instance
                      ON instance.company_id = subject.company_id
                     AND instance.principal_id = subject.principal_id
                    JOIN public.anc_agent_runs AS run
                      ON run.company_id = instance.company_id
                     AND run.agent_instance_id = instance.agent_instance_id
                    WHERE subject.company_id = %(company_id)s
                      AND subject.external_subject = %(external_subject)s
                      AND subject.oauth_client_id = %(oauth_client_id)s
                    """,
                    identity.parameters,
                ).fetchone()

        assert row is not None
        assert row[:9] == (
            identity.auth_subject_id,
            SPECIALIST_PRINCIPAL_ID,
            SPECIALIST_INSTANCE_ID,
            identity.definition_id,
            identity.definition_version,
            "specialist",
            CELL_ID,
            RUN_ID,
            "running",
        )
        assert row[9].astimezone(UTC) == identity.run_expires_at
        assert row[10].astimezone(UTC) == identity.provisioned_at

        authority = LocalTestAuthenticator(
            {
                "closed-beta-e2e": PrincipalRecord(
                    principal_id=SPECIALIST_PRINCIPAL_ID,
                    grants=(
                        ScopedRoleGrant(
                            Role.SPECIALIST,
                            ResourceScope.company(COMPANY_ID),
                        ),
                    ),
                    agent_run_id=RUN_ID,
                    agent_identity=AgentIdentityContext(
                        agent_instance_id=SPECIALIST_INSTANCE_ID,
                        definition_id=identity.definition_id,
                        definition_version=identity.definition_version,
                        role=Role.SPECIALIST,
                        cell_id=CELL_ID,
                    ),
                )
            }
        ).authenticate("closed-beta-e2e")
        described = await create_service(store=repository).invoke(
            "company.describe",
            authority,
            {},
        )

        assert described["principal"] == {
            "principal_id": SPECIALIST_PRINCIPAL_ID,
            "roles": ["specialist"],
            "effective_capabilities": sorted(
                next(
                    agent.definition["declared_capabilities"]
                    for agent in desired_bootstrap.agents
                    if agent.role is Role.SPECIALIST
                )
            ),
            "agent_instance_id": SPECIALIST_INSTANCE_ID,
            "definition_id": identity.definition_id,
            "definition_version": identity.definition_version,
            "role": "specialist",
            "cell_id": CELL_ID,
            "agent_run_id": RUN_ID,
        }


def _rotation_spec(
    manifest: BootstrapManifest,
    *,
    agent_run_id: str,
    occurred_at: datetime,
) -> ProvisioningSpec:
    stamp = occurred_at.isoformat().replace("+00:00", "Z")
    return _identity_spec(
        manifest,
        company_id=ROTATION_COMPANY_ID,
        cell_id=ROTATION_CELL_ID,
        principal_id=ROTATION_SPECIALIST_PRINCIPAL_ID,
        instance_id=ROTATION_SPECIALIST_INSTANCE_ID,
        oauth_client_id=ROTATION_CLIENT_ID,
        external_subject=ROTATION_SUBJECT_ID,
        agent_run_id=agent_run_id,
        rotate=True,
        run_created_at=stamp,
        run_started_at=stamp,
        run_expires_at=(occurred_at + timedelta(days=7)).isoformat().replace("+00:00", "Z"),
        rotation_kind="expiry",
        now=occurred_at + timedelta(seconds=1),
    )


def _run_rows(connection: object) -> list[tuple[object, ...]]:
    return connection.execute(
        """
        SELECT agent_run_id, status, termination_code, started_at, expires_at,
               ended_at, created_at
        FROM public.anc_agent_runs
        WHERE company_id = %(company_id)s
          AND agent_instance_id = %(instance_id)s
        ORDER BY created_at, agent_run_id
        """,
        {
            "company_id": ROTATION_COMPANY_ID,
            "instance_id": ROTATION_SPECIALIST_INSTANCE_ID,
        },
    ).fetchall()


async def test_expired_run_rotation_retires_predecessor_and_keeps_one_successor() -> None:
    psycopg = pytest.importorskip("psycopg")
    psycopg_pool = pytest.importorskip("psycopg_pool")
    assert DATABASE_URL is not None

    pool, desired_bootstrap, identity = await _prepare_closed_beta_identity(
        psycopg,
        psycopg_pool,
        mapping=_rotation_bootstrap_mapping(),
        identity_kwargs={
            "company_id": ROTATION_COMPANY_ID,
            "cell_id": ROTATION_CELL_ID,
            "principal_id": ROTATION_SPECIALIST_PRINCIPAL_ID,
            "instance_id": ROTATION_SPECIALIST_INSTANCE_ID,
            "oauth_client_id": ROTATION_CLIENT_ID,
            "external_subject": ROTATION_SUBJECT_ID,
            "agent_run_id": ROTATION_RUN_ID,
            # Issuance fields are immutable. Insert the predecessor already
            # past its window (valid at spec now, expired at wall-clock now).
            "run_expires_at": "2026-08-30T02:00:00Z",
        },
    )
    async with pool:
        repository = PostgresRepository(pool=pool)
        await apply_bootstrap(desired_bootstrap, repository)
        with _operator_connection(psycopg, company_id=ROTATION_COMPANY_ID) as operator:
            apply_provisioning(identity, operator)
            occurred_at = datetime.now(UTC).replace(microsecond=0)

            def _attempt(run_id: str) -> str | None:
                with _operator_connection(
                    psycopg, company_id=ROTATION_COMPANY_ID
                ) as racing_operator:
                    spec = _rotation_spec(
                        desired_bootstrap,
                        agent_run_id=run_id,
                        occurred_at=occurred_at,
                    )
                    try:
                        apply_rotation(
                            spec,
                            racing_operator,
                            now=occurred_at + timedelta(seconds=1),
                        )
                    except ProvisioningError:
                        return None
                    return run_id

            with ThreadPoolExecutor(max_workers=2) as pool_workers:
                outcomes = list(
                    pool_workers.map(_attempt, (ROTATION_SUCCESSOR_RUN_ID, ROTATION_RACING_RUN_ID))
                )
            applied = [run_id for run_id in outcomes if run_id is not None]
            assert len(applied) == 1
            winner_id = applied[0]
            rows = _run_rows(operator)
            by_id = {row[0]: row for row in rows}
            assert by_id[ROTATION_RUN_ID][1] == "expired"
            assert by_id[ROTATION_RUN_ID][2] == "run_expired"
            assert by_id[winner_id][1] == "running"
            assert by_id[winner_id][2] is None
            assert by_id[winner_id][6].astimezone(UTC) == occurred_at
            assert sum(1 for row in rows if row[1] == "running") == 1

            subject = operator.execute(
                """
                SELECT token_valid_after, created_at
                FROM public.anc_auth_subjects
                WHERE company_id = %(company_id)s AND auth_subject_id = %(subject_id)s
                """,
                {
                    "company_id": ROTATION_COMPANY_ID,
                    "subject_id": identity.auth_subject_id,
                },
            ).fetchone()
            assert subject is not None
            assert subject[0].astimezone(UTC) == identity.provisioned_at
            assert subject[1].astimezone(UTC) == identity.provisioned_at

            winner = _rotation_spec(
                desired_bootstrap,
                agent_run_id=winner_id,
                occurred_at=occurred_at,
            )
            again = apply_rotation(winner, operator, now=occurred_at + timedelta(seconds=2))
            assert again["agent_run_id"] == winner_id
            assert sum(1 for row in _run_rows(operator) if row[1] == "running") == 1

            loser_id = (
                ROTATION_RACING_RUN_ID
                if winner_id == ROTATION_SUCCESSOR_RUN_ID
                else ROTATION_SUCCESSOR_RUN_ID
            )
            racing = _rotation_spec(
                desired_bootstrap,
                agent_run_id=loser_id,
                occurred_at=occurred_at + timedelta(seconds=3),
            )
            with pytest.raises(ProvisioningError, match="non-expired competing run"):
                apply_rotation(racing, operator, now=occurred_at + timedelta(seconds=4))
