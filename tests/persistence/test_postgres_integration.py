from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from mcp.server.auth.provider import AccessToken

from ai_nature_company.auth import AgentRunExpiredError
from ai_nature_company.maintenance import scan_running_agent_runs
from ai_nature_company.postgres_identity import PostgresSubjectIdentityRepository
from ai_nature_company.supabase_auth import (
    InMemorySubjectIdentityRepository,
    SupabaseIdentityAuthenticator,
)

DATABASE_URL = os.getenv("ANC_TEST_DATABASE_URL")
RUNTIME_ROLE = "ai_nature_company_runtime"
RUNTIME_APPEND_ONLY_TABLES = {
    "anc_domain_events",
    "anc_audit_events",
    "anc_mcp_tool_calls",
}
RUNTIME_MUTABLE_TABLES = {
    "anc_stream_heads",
    "anc_projections",
    "anc_idempotency_records",
    "anc_budgets",
    "anc_budget_reservations",
    "anc_work_leases",
}
RUNTIME_IDENTITY_TABLES = {
    "anc_oauth_clients",
    "anc_oauth_client_resource_aliases",
    "anc_auth_subjects",
    "anc_agent_instances",
    "anc_agent_runs",
}
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="set ANC_TEST_DATABASE_URL to run real PostgreSQL repository tests",
)


async def test_real_postgres_repository_contract() -> None:
    psycopg = pytest.importorskip("psycopg")
    psycopg_pool = pytest.importorskip("psycopg_pool")
    from psycopg import sql

    from ai_nature_company.postgres import PostgresRepository

    assert DATABASE_URL is not None
    migrations = [path.read_text() for path in sorted(Path("supabase/migrations").glob("*.sql"))]
    role_name = f"anc_test_{uuid4().hex}"
    admin_user: str
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as connection:
        for migration in migrations:
            await connection.execute(migration)
        cursor = await connection.execute(
            """
            SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolinherit
            FROM pg_catalog.pg_roles
            WHERE rolname = %s
            """,
            (RUNTIME_ROLE,),
        )
        assert await cursor.fetchone() == (False, False, False, False, False)

        expected_privileges = {
            **{table: (True, True, False, False, False) for table in RUNTIME_APPEND_ONLY_TABLES},
            **{table: (True, True, True, False, False) for table in RUNTIME_MUTABLE_TABLES},
            **{table: (False, False, False, False, False) for table in RUNTIME_IDENTITY_TABLES},
        }
        for table, expected in expected_privileges.items():
            cursor = await connection.execute(
                """
                SELECT
                    has_table_privilege(%s, %s, 'SELECT'),
                    has_table_privilege(%s, %s, 'INSERT'),
                    has_table_privilege(%s, %s, 'UPDATE'),
                    has_table_privilege(%s, %s, 'DELETE'),
                    has_table_privilege(%s, %s, 'TRUNCATE')
                """,
                (
                    RUNTIME_ROLE,
                    f"public.{table}",
                    RUNTIME_ROLE,
                    f"public.{table}",
                    RUNTIME_ROLE,
                    f"public.{table}",
                    RUNTIME_ROLE,
                    f"public.{table}",
                    RUNTIME_ROLE,
                    f"public.{table}",
                ),
            )
            assert await cursor.fetchone() == expected

        for sequence in (
            "anc_domain_events_position_seq",
            "anc_audit_events_position_seq",
            "anc_mcp_tool_calls_position_seq",
        ):
            cursor = await connection.execute(
                """
                SELECT
                    has_sequence_privilege(%s, %s, 'USAGE'),
                    has_sequence_privilege(%s, %s, 'SELECT'),
                    has_sequence_privilege(%s, %s, 'UPDATE')
                """,
                (
                    RUNTIME_ROLE,
                    f"public.{sequence}",
                    RUNTIME_ROLE,
                    f"public.{sequence}",
                    RUNTIME_ROLE,
                    f"public.{sequence}",
                ),
            )
            assert await cursor.fetchone() == (True, True, False)

        cursor = await connection.execute(
            """
            SELECT has_function_privilege(
                %s,
                'public.anc_resolve_agent_identity(text,text,text,text,text)',
                'EXECUTE'
            ),
            has_function_privilege(
                %s,
                'public.anc_inspect_agent_run_binding(text,text,text,text,text)',
                'EXECUTE'
            ),
            has_function_privilege(
                %s,
                'public.anc_list_expiring_agent_runs()',
                'EXECUTE'
            ),
            has_function_privilege(
                %s,
                'public.anc_list_tick_company_ids()',
                'EXECUTE'
            ),
            has_function_privilege(
                %s,
                'public.anc_list_maintenance_agent_runs(text)',
                'EXECUTE'
            ),
            has_function_privilege(
                %s,
                'public.anc_end_cell_agent_runs(text,text,text,timestamptz)',
                'EXECUTE'
            ),
            has_function_privilege(
                'ai_nature_company_operator',
                'public.anc_end_cell_agent_runs(text,text,text,timestamptz)',
                'EXECUTE'
            )
            """,
            (RUNTIME_ROLE,) * 6,
        )
        assert await cursor.fetchone() == (True, True, True, True, True, True, False)

        governed_tables = (
            RUNTIME_APPEND_ONLY_TABLES | RUNTIME_MUTABLE_TABLES | RUNTIME_IDENTITY_TABLES
        )
        cursor = await connection.execute(
            """
            SELECT relation.relname, relation.relrowsecurity, relation.relforcerowsecurity
            FROM pg_catalog.pg_class AS relation
            JOIN pg_catalog.pg_namespace AS namespace
              ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'public' AND relation.relname = ANY(%s)
            ORDER BY relation.relname
            """,
            (list(governed_tables),),
        )
        assert await cursor.fetchall() == [(table, True, True) for table in sorted(governed_tables)]
        cursor = await connection.execute("SELECT current_user")
        row = await cursor.fetchone()
        assert row is not None
        admin_user = row[0]
        await connection.execute(
            sql.SQL(
                "CREATE ROLE {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOLOGIN NOBYPASSRLS"
            ).format(sql.Identifier(role_name))
        )
        await connection.execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(role_name),
                sql.Identifier(admin_user),
            )
        )
        await connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role_name))
        )
        await connection.execute(
            sql.SQL(
                "GRANT SELECT, INSERT ON TABLE "
                "public.anc_domain_events, public.anc_audit_events TO {}"
            ).format(sql.Identifier(role_name))
        )
        await connection.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE ON TABLE "
                "public.anc_stream_heads, public.anc_projections, "
                "public.anc_idempotency_records, public.anc_budgets, "
                "public.anc_budget_reservations, public.anc_work_leases TO {}"
            ).format(sql.Identifier(role_name))
        )
        await connection.execute(
            sql.SQL(
                "GRANT USAGE, SELECT ON SEQUENCE "
                "public.anc_domain_events_position_seq, "
                "public.anc_audit_events_position_seq TO {}"
            ).format(sql.Identifier(role_name))
        )
        cursor = await connection.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s",
            (role_name,),
        )
        assert await cursor.fetchone() == (False, False)

    async def assume_runtime_role(connection: object) -> None:
        await connection.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role_name)))
        await connection.execute("SET row_security = on")
        await connection.execute("SELECT set_config('app.company_id', '', false)")
        await connection.commit()

    company_id = f"test-{uuid4()}"
    other_company_id = f"test-{uuid4()}"
    now = datetime.now(UTC)

    # Passing privileged credentials directly to the repository must fail closed.
    # Migrations may use the admin DSN, but the runtime substrate may not.
    unsafe_pool = psycopg_pool.AsyncConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=1,
        open=False,
    )
    async with unsafe_pool:
        unsafe_repository = PostgresRepository(pool=unsafe_pool)
        with pytest.raises(
            RuntimeError,
            match="database runtime role must be NOSUPERUSER and NOBYPASSRLS",
        ):
            async with unsafe_repository.transaction(company_id):
                pytest.fail("a privileged database role reached the repository transaction body")

    pool = psycopg_pool.AsyncConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=2,
        open=False,
        configure=assume_runtime_role,
    )
    try:
        async with pool:
            repository = PostgresRepository(pool=pool)
            async with repository.transaction(company_id) as transaction:
                event = await transaction.append_event(
                    aggregate_type="offer",
                    aggregate_id="offer-1",
                    event_type="offer.created",
                    actor_id="agent-1",
                    payload={"price": "5000.00"},
                    event_id="event-1",
                    expected_revision=0,
                    occurred_at=now,
                )
                projection = await transaction.put_projection(
                    projection_type="offer",
                    projection_id="offer-1",
                    state={"status": "active", "cell_id": "cell-a"},
                    expected_revision=0,
                    updated_at=now,
                )
                assert await transaction.list_projections(
                    projection_type="offer", cell_id="cell-a", limit=1
                ) == [projection]
                assert (
                    await transaction.list_projections(
                        projection_type="offer", cell_id="cell-b", limit=1
                    )
                    == []
                )
                assert (
                    await transaction.list_projections(
                        projection_type="offer", cell_id="cell-a", after_projection_id="offer-1"
                    )
                    == []
                )
                await transaction.create_budget(
                    budget_id="budget-1",
                    scope="company",
                    limit="100",
                    currency="USD",
                    starts_at=now,
                    ends_at=now + timedelta(days=1),
                    now=now,
                )
                await transaction.reserve_budget(
                    budget_id="budget-1",
                    reservation_id="reservation-1",
                    amount="25",
                    purpose="test",
                    expected_revision=0,
                    now=now,
                )
                hierarchy_company = await transaction.create_budget(
                    budget_id="hierarchy-company",
                    scope="company",
                    limit="50",
                    currency="USD",
                    starts_at=now,
                    ends_at=now + timedelta(days=1),
                    now=now,
                )
                hierarchy_stage = await transaction.create_budget(
                    budget_id="hierarchy-stage",
                    scope="stage",
                    parent_budget_id=hierarchy_company["budget_id"],
                    stage="exploration",
                    limit="10",
                    currency="USD",
                    starts_at=now,
                    ends_at=now + timedelta(days=1),
                    now=now,
                )
                hierarchy_cell = await transaction.create_budget(
                    budget_id="hierarchy-cell",
                    scope="cell",
                    parent_budget_id=hierarchy_company["budget_id"],
                    cell_id="cell-1",
                    limit="20",
                    currency="USD",
                    starts_at=now,
                    ends_at=now + timedelta(days=1),
                    now=now,
                )
                group = await transaction.reserve_budget_group(
                    reservation_group_id="hierarchy-hold",
                    budget_ids={
                        "company": hierarchy_company["budget_id"],
                        "stage": hierarchy_stage["budget_id"],
                        "cell": hierarchy_cell["budget_id"],
                    },
                    amount="5",
                    purpose="integration hierarchy",
                    now=now,
                )
                spent = await transaction.apply_reservation_group_spend(
                    reservation_group_id="hierarchy-hold",
                    amount="1",
                    expected_revisions={
                        member["reservation_id"]: member["revision"] for member in group["members"]
                    },
                    now=now,
                )
                assert spent["status"] == "active"
                assert spent["actual_amount"] == Decimal("1")
                resized = await transaction.adjust_reservation_group_hold(
                    reservation_group_id="hierarchy-hold",
                    amount="8",
                    expected_revisions={
                        member["reservation_id"]: member["revision"] for member in spent["members"]
                    },
                    metadata={"amount_minor": 8},
                    now=now,
                )
                assert resized["status"] == "active"
                assert resized["amount"] == Decimal("8")
                assert resized["actual_amount"] == Decimal("1")
                lease = await transaction.acquire_lease(
                    work_item_id="work-1",
                    agent_id="agent-1",
                    agent_run_id="run-1",
                    ttl=60,
                    now=now,
                )

            async with repository.transaction(other_company_id) as transaction:
                await transaction.put_projection(
                    projection_type="offer",
                    projection_id="offer-2",
                    state={"status": "private"},
                    expected_revision=0,
                    updated_at=now,
                )

            exported = await repository.export_company(company_id)
            isolated = await repository.export_company(other_company_id)
            replayed = await repository.replay(
                company_id,
                lambda state, item: [*state, item["event_type"]],
                [],
            )

            # A runtime connection without app.company_id fails closed for reads.
            async with pool.connection() as connection, connection.transaction():
                cursor = await connection.execute("SELECT count(*) FROM public.anc_projections")
                assert (await cursor.fetchone())[0] == 0

            # A missing scope also blocks writes through the policy's WITH CHECK.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with pool.connection() as connection, connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO public.anc_projections
                            (company_id, projection_type, projection_id, revision,
                             state, created_at, updated_at)
                        VALUES (%s, 'offer', 'unscoped', 1, '{}'::jsonb, %s, %s)
                        """,
                        (company_id, now, now),
                    )

            # A company-A scope cannot observe company-B records, even when a direct
            # query deliberately omits the company predicate.
            async with pool.connection() as connection, connection.transaction():
                await connection.execute(
                    "SELECT set_config('app.company_id', %s, true)", (company_id,)
                )
                cursor = await connection.execute(
                    "SELECT company_id, projection_id FROM public.anc_projections "
                    "ORDER BY projection_id"
                )
                assert await cursor.fetchall() == [(company_id, "offer-1")]
                cursor = await connection.execute(
                    "SELECT count(*) FROM public.anc_projections WHERE company_id = %s",
                    (other_company_id,),
                )
                assert (await cursor.fetchone())[0] == 0

            # RLS WITH CHECK blocks an attempted cross-company write.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with pool.connection() as connection, connection.transaction():
                    await connection.execute(
                        "SELECT set_config('app.company_id', %s, true)", (company_id,)
                    )
                    await connection.execute(
                        """
                        INSERT INTO public.anc_projections
                            (company_id, projection_type, projection_id, revision,
                             state, created_at, updated_at)
                        VALUES (%s, 'offer', 'forbidden', 1, '{}'::jsonb, %s, %s)
                        """,
                        (other_company_id, now, now),
                    )

            # The runtime role has no UPDATE or DELETE authority over the append-only
            # event log, independently of its company scope.
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with pool.connection() as connection, connection.transaction():
                    await connection.execute(
                        "SELECT set_config('app.company_id', %s, true)", (company_id,)
                    )
                    await connection.execute(
                        "UPDATE public.anc_domain_events "
                        "SET payload = '{\"tampered\": true}'::jsonb "
                        "WHERE company_id = %s AND event_id = 'event-1'",
                        (company_id,),
                    )

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                async with pool.connection() as connection, connection.transaction():
                    await connection.execute(
                        "SELECT set_config('app.company_id', %s, true)", (company_id,)
                    )
                    await connection.execute(
                        "DELETE FROM public.anc_domain_events "
                        "WHERE company_id = %s AND event_id = 'event-1'",
                        (company_id,),
                    )

            survived = await repository.export_company(company_id)
            assert [item["event_id"] for item in survived["domain_events"]] == ["event-1"]

        assert event["aggregate_revision"] == 1
        assert projection["revision"] == 1
        assert lease["status"] == "active"
        assert lease["agent_run_id"] == "run-1"
        assert len(exported["domain_events"]) == 1
        assert exported["work_leases"][0]["agent_run_id"] == "run-1"
        assert "token" not in exported["work_leases"][0]
        assert set(group["reservation_ids"]) == {"company", "cell", "stage"}
        hierarchy_members = [
            item
            for item in exported["reservations"]
            if item["reservation_group_id"] == "hierarchy-hold"
        ]
        assert len(hierarchy_members) == 3
        assert {item["ledger_role"] for item in hierarchy_members} == {
            "company",
            "cell",
            "stage",
        }
        assert len(isolated["projections"]) == 1
        assert replayed == ["offer.created"]
    finally:
        if not pool.closed:
            await pool.close()
        async with await psycopg.AsyncConnection.connect(
            DATABASE_URL, autocommit=True
        ) as connection:
            await connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role_name)))
            await connection.execute(
                sql.SQL("REVOKE {} FROM {}").format(
                    sql.Identifier(role_name),
                    sql.Identifier(admin_user),
                )
            )
            await connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name)))


async def test_supabase_public_client_id_and_session_subject_resolver_contract() -> None:
    """Exercise the resolver against the minimum relevant Supabase auth catalog.

    This test is intentionally limited to a disposable PostgreSQL database.  A
    pre-existing ``auth`` schema may be a real Supabase-managed catalog, so the
    test skips before applying migrations or changing any privileges in that
    schema.
    """

    psycopg = pytest.importorskip("psycopg")

    assert DATABASE_URL is not None
    migrations = [path.read_text() for path in sorted(Path("supabase/migrations").glob("*.sql"))]
    oauth_client_id = uuid4()
    public_client_id = str(oauth_client_id)
    subject_id = uuid4()
    other_subject_id = uuid4()
    session_id = uuid4()
    other_session_id = uuid4()
    suffix = uuid4().hex
    company_id = f"resolver-company-{suffix}"
    auth_subject_id = f"resolver-subject-{suffix}"
    principal_id = f"resolver-principal-{suffix}"
    instance_id = f"resolver-instance-{suffix}"
    run_id = f"resolver-run-{suffix}"
    resource_url = "https://substrate.example.test/mcp"
    issuer = "https://project-ref.supabase.co/auth/v1"
    created_at = datetime.now(UTC) - timedelta(minutes=2)
    token_valid_after = created_at + timedelta(seconds=30)
    session_created_at = created_at + timedelta(minutes=1)
    run_expires_at = created_at + timedelta(hours=1)

    async with await psycopg.AsyncConnection.connect(
        DATABASE_URL,
        autocommit=True,
    ) as connection:
        cursor = await connection.execute("SELECT to_regnamespace('auth') IS NOT NULL")
        if (await cursor.fetchone())[0]:
            pytest.skip("refusing to create fixtures in a database with an existing auth schema")

        await connection.execute("CREATE SCHEMA auth")
        try:
            await connection.execute(
                """
                CREATE TABLE auth.oauth_clients (
                    id uuid PRIMARY KEY,
                    deleted_at timestamptz
                )
                """
            )
            await connection.execute(
                """
                CREATE TABLE auth.sessions (
                    id uuid PRIMARY KEY,
                    user_id uuid NOT NULL,
                    oauth_client_id uuid NOT NULL
                        REFERENCES auth.oauth_clients (id),
                    not_after timestamptz,
                    created_at timestamptz NOT NULL
                )
                """
            )
            await connection.execute(
                """
                CREATE TABLE auth.refresh_tokens (
                    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    session_id uuid NOT NULL REFERENCES auth.sessions (id),
                    revoked boolean NOT NULL
                )
                """
            )

            for migration in migrations:
                await connection.execute(migration)

            compatibility_cursor = await connection.execute(
                "SELECT public.anc_auth_catalog_compatible()"
            )
            assert await compatibility_cursor.fetchone() == (True,)

            await connection.execute(
                """
                INSERT INTO auth.oauth_clients (id, deleted_at)
                VALUES (%s, NULL)
                """,
                (oauth_client_id,),
            )
            await connection.execute(
                """
                INSERT INTO auth.sessions
                    (id, user_id, oauth_client_id, not_after, created_at)
                VALUES
                    (%s, %s, %s, %s, %s),
                    (%s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    subject_id,
                    oauth_client_id,
                    run_expires_at,
                    session_created_at,
                    other_session_id,
                    other_subject_id,
                    oauth_client_id,
                    run_expires_at,
                    session_created_at,
                ),
            )
            await connection.execute(
                """
                INSERT INTO auth.refresh_tokens (session_id, revoked)
                VALUES (%s, false), (%s, false)
                """,
                (session_id, other_session_id),
            )
            await connection.execute(
                """
                INSERT INTO public.anc_oauth_clients
                    (client_id, company_id, resource_url, display_name, status,
                     revision, created_at, updated_at)
                VALUES (%s, %s, %s, 'Resolver integration client', 'active',
                        0, %s, %s)
                """,
                (public_client_id, company_id, resource_url, created_at, created_at),
            )
            await connection.execute(
                """
                INSERT INTO public.anc_auth_subjects
                    (company_id, auth_subject_id, issuer, external_subject,
                     oauth_client_id, principal_id, subject_kind, status,
                     token_valid_after, disabled_at, revision, created_at, updated_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, 'agent', 'active',
                     %s, NULL, 0, %s, %s)
                """,
                (
                    company_id,
                    auth_subject_id,
                    issuer,
                    str(subject_id),
                    public_client_id,
                    principal_id,
                    token_valid_after,
                    created_at,
                    created_at,
                ),
            )
            await connection.execute(
                """
                INSERT INTO public.anc_agent_instances
                    (company_id, agent_instance_id, principal_id, definition_id,
                     definition_version, role, cell_id, status, revision,
                     created_at, updated_at)
                VALUES
                    (%s, %s, %s, 'specialist', 1, 'specialist', 'cell-1',
                     'active', 0, %s, %s)
                """,
                (company_id, instance_id, principal_id, created_at, created_at),
            )
            await connection.execute(
                """
                INSERT INTO public.anc_agent_runs
                    (company_id, agent_run_id, agent_instance_id, principal_id,
                     definition_id, definition_version, role, cell_id, status,
                     termination_code, revision, started_at, expires_at, ended_at,
                     created_at, updated_at)
                VALUES
                    (%s, %s, %s, %s, 'specialist', 1, 'specialist', 'cell-1',
                     'running', NULL, 0, %s, %s, NULL, %s, %s)
                """,
                (
                    company_id,
                    run_id,
                    instance_id,
                    principal_id,
                    session_created_at,
                    run_expires_at,
                    created_at,
                    created_at,
                ),
            )

            async def resolve(
                *, client_id: str, subject: str, session: str
            ) -> list[tuple[object, ...]]:
                resolver_cursor = await connection.execute(
                    """
                    SELECT *
                    FROM public.anc_resolve_agent_identity(%s, %s, %s, %s, %s)
                    """,
                    (issuer, subject, client_id, resource_url, session),
                )
                return await resolver_cursor.fetchall()

            async def inspect(
                *, client_id: str, subject: str, session: str
            ) -> list[tuple[object, ...]]:
                inspect_cursor = await connection.execute(
                    """
                    SELECT *
                    FROM public.anc_inspect_agent_run_binding(%s, %s, %s, %s, %s)
                    """,
                    (issuer, subject, client_id, resource_url, session),
                )
                return await inspect_cursor.fetchall()

            await connection.execute("SET ROLE ai_nature_company_runtime")
            try:
                resolved = await resolve(
                    client_id=public_client_id,
                    subject=str(subject_id),
                    session=str(session_id),
                )
                assert len(resolved) == 1
                assert resolved[0][0:4] == (
                    company_id,
                    auth_subject_id,
                    principal_id,
                    "agent",
                )
                assert resolved[0][5:12] == (
                    instance_id,
                    "specialist",
                    1,
                    "specialist",
                    "cell-1",
                    run_id,
                    run_expires_at,
                )

                live_binding = PostgresSubjectIdentityRepository._binding_from_row(
                    resolved[0],
                    issuer=issuer,
                    subject=str(subject_id),
                    client_id=public_client_id,
                )
                assert live_binding is not None
                live_auth = SupabaseIdentityAuthenticator(
                    InMemorySubjectIdentityRepository([live_binding]),
                    resource_server_url=resource_url,
                )
                live_principal = await live_auth(
                    AccessToken(
                        token="secret-bearer-token",
                        client_id=public_client_id,
                        scopes=["openid"],
                        subject=str(subject_id),
                        resource=resource_url,
                        claims={
                            "iss": issuer,
                            "aud": "authenticated",
                            "iat": int(session_created_at.timestamp()),
                            "session_id": str(session_id),
                        },
                    )
                )
                assert live_principal.principal_id == principal_id
                assert live_principal.agent_run_id == run_id
                assert live_principal.agent_run_expires_at == run_expires_at
                expiring = await connection.execute(
                    "SELECT agent_run_id FROM public.anc_list_expiring_agent_runs()"
                )
                expiring_ids = [row[0] for row in await expiring.fetchall()]
                assert run_id in expiring_ids

                await connection.execute("RESET ROLE")

                async def retire_running_and_insert(
                    *,
                    started_at: datetime,
                    expires_at: datetime,
                ) -> str:
                    """Issuance fields are immutable; a new window is a new run."""

                    new_run_id = f"resolver-run-{uuid4().hex}"
                    await connection.execute(
                        """
                        UPDATE public.anc_agent_runs
                        SET status = 'expired',
                            ended_at = GREATEST(now(), started_at),
                            termination_code = 'run_expired',
                            revision = revision + 1,
                            updated_at = GREATEST(now(), started_at)
                        WHERE company_id = %s
                          AND agent_instance_id = %s
                          AND status = 'running'
                        """,
                        (company_id, instance_id),
                    )
                    await connection.execute(
                        """
                        INSERT INTO public.anc_agent_runs
                            (company_id, agent_run_id, agent_instance_id, principal_id,
                             definition_id, definition_version, role, cell_id, status,
                             termination_code, revision, started_at, expires_at, ended_at,
                             created_at, updated_at)
                        VALUES
                            (%s, %s, %s, %s, 'specialist', 1, 'specialist', 'cell-1',
                             'running', NULL, 0, %s, %s, NULL, %s, %s)
                        """,
                        (
                            company_id,
                            new_run_id,
                            instance_id,
                            principal_id,
                            started_at,
                            expires_at,
                            created_at,
                            created_at,
                        ),
                    )
                    return new_run_id

                future_start = datetime.now(UTC) + timedelta(minutes=5)
                await retire_running_and_insert(
                    started_at=future_start,
                    expires_at=future_start + timedelta(hours=1),
                )
                await connection.execute("SET ROLE ai_nature_company_runtime")
                assert not await resolve(
                    client_id=public_client_id,
                    subject=str(subject_id),
                    session=str(session_id),
                )

                await connection.execute("RESET ROLE")
                exact_start = datetime.now(UTC)
                exact_start_run_id = await retire_running_and_insert(
                    started_at=exact_start,
                    expires_at=exact_start + timedelta(hours=1),
                )
                await connection.execute("SET ROLE ai_nature_company_runtime")
                at_start = await resolve(
                    client_id=public_client_id,
                    subject=str(subject_id),
                    session=str(session_id),
                )
                assert len(at_start) == 1
                assert at_start[0][10] == exact_start_run_id

                await connection.execute("RESET ROLE")
                after_start_run_id = await retire_running_and_insert(
                    started_at=exact_start - timedelta(seconds=1),
                    expires_at=exact_start + timedelta(hours=1),
                )
                await connection.execute("SET ROLE ai_nature_company_runtime")
                after_start = await resolve(
                    client_id=public_client_id,
                    subject=str(subject_id),
                    session=str(session_id),
                )
                assert len(after_start) == 1
                assert after_start[0][10] == after_start_run_id

                await connection.execute("RESET ROLE")
                exact_expiry = datetime.now(UTC)
                expired_run_id = await retire_running_and_insert(
                    started_at=session_created_at,
                    expires_at=exact_expiry,
                )
                await connection.execute("SET ROLE ai_nature_company_runtime")
                assert not await resolve(
                    client_id=public_client_id,
                    subject=str(subject_id),
                    session=str(session_id),
                )
                expired = await inspect(
                    client_id=public_client_id,
                    subject=str(subject_id),
                    session=str(session_id),
                )
                assert len(expired) == 1
                assert expired[0][10] == expired_run_id
                assert expired[0][12] == "running"
                assert expired[0][13] == "active"

                expired_binding = PostgresSubjectIdentityRepository._binding_from_row(
                    expired[0],
                    issuer=issuer,
                    subject=str(subject_id),
                    client_id=public_client_id,
                )
                assert expired_binding is not None
                expired_auth = SupabaseIdentityAuthenticator(
                    InMemorySubjectIdentityRepository([expired_binding]),
                    resource_server_url=resource_url,
                )
                with pytest.raises(AgentRunExpiredError) as expired_error:
                    await expired_auth(
                        AccessToken(
                            token="secret-bearer-token",
                            client_id=public_client_id,
                            scopes=["openid"],
                            subject=str(subject_id),
                            resource=resource_url,
                            claims={
                                "iss": issuer,
                                "aud": "authenticated",
                                "iat": int(session_created_at.timestamp()),
                                "session_id": str(session_id),
                            },
                        )
                    )
                assert expired_error.value.code == "run_expired"
                assert expired_error.value.agent_run_id == expired_run_id

                await connection.execute("RESET ROLE")
                open_window_run_id = await retire_running_and_insert(
                    started_at=exact_expiry - timedelta(seconds=1),
                    expires_at=exact_expiry + timedelta(minutes=5),
                )
                await connection.execute("SET ROLE ai_nature_company_runtime")
                after_expiry_window = await resolve(
                    client_id=public_client_id,
                    subject=str(subject_id),
                    session=str(session_id),
                )
                assert len(after_expiry_window) == 1
                assert after_expiry_window[0][10] == open_window_run_id

                await connection.execute("RESET ROLE")
                await connection.execute(
                    """
                    UPDATE public.anc_agent_runs
                    SET status = 'revoked',
                        ended_at = GREATEST(now(), started_at),
                        termination_code = 'operator_revoked',
                        revision = revision + 1,
                        updated_at = GREATEST(now(), started_at)
                    WHERE company_id = %s
                      AND agent_run_id = %s
                    """,
                    (company_id, open_window_run_id),
                )
                await connection.execute("SET ROLE ai_nature_company_runtime")
                assert not await resolve(
                    client_id=public_client_id,
                    subject=str(subject_id),
                    session=str(session_id),
                )
                revoked_rows = await inspect(
                    client_id=public_client_id,
                    subject=str(subject_id),
                    session=str(session_id),
                )
                assert len(revoked_rows) == 1
                assert revoked_rows[0][10] == open_window_run_id
                assert revoked_rows[0][12] == "revoked"
                assert revoked_rows[0][13] == "active"

                # In the current Supabase catalog, the UUID primary key is both
                # the public JWT ``client_id`` and the session foreign key.
                assert not await resolve(
                    client_id=str(uuid4()),
                    subject=str(subject_id),
                    session=str(session_id),
                )
                # A real session for the same OAuth client still fails when its
                # user does not equal the JWT subject bound to this agent.
                assert not await resolve(
                    client_id=public_client_id,
                    subject=str(subject_id),
                    session=str(other_session_id),
                )
                assert not await resolve(
                    client_id=public_client_id,
                    subject=str(other_subject_id),
                    session=str(session_id),
                )
                assert not await resolve(
                    client_id=public_client_id,
                    subject=str(subject_id),
                    session=str(uuid4()),
                )

                # The legacy catalog exposed a separate public ``client_id``.
                # It may still exist during a staggered upgrade, but the resolver
                # must ignore it and return no identity rather than authenticating
                # the obsolete identifier.
                legacy_client_id = f"legacy-public-{uuid4().hex}"
                await connection.execute("RESET ROLE")
                await connection.execute("ALTER TABLE auth.oauth_clients ADD COLUMN client_id text")
                await connection.execute(
                    "UPDATE auth.oauth_clients SET client_id = %s WHERE id = %s",
                    (legacy_client_id, oauth_client_id),
                )
                await connection.execute("SET ROLE ai_nature_company_runtime")
                assert not await resolve(
                    client_id=legacy_client_id,
                    subject=str(subject_id),
                    session=str(session_id),
                )

                # Missing or type-incompatible required catalog columns are
                # detected before the dynamic query is parsed.
                await connection.execute("RESET ROLE")
                await connection.execute("ALTER TABLE auth.oauth_clients DROP COLUMN deleted_at")
                await connection.execute("SET ROLE ai_nature_company_runtime")
                compatibility_cursor = await connection.execute(
                    "SELECT public.anc_auth_catalog_compatible()"
                )
                assert await compatibility_cursor.fetchone() == (False,)
                assert not await resolve(
                    client_id=public_client_id,
                    subject=str(subject_id),
                    session=str(session_id),
                )
            finally:
                await connection.execute("RESET ROLE")
        finally:
            await connection.execute("DROP SCHEMA auth CASCADE")


async def test_runtime_role_schema_fingerprint_passes_after_all_shipped_migrations() -> None:
    psycopg = pytest.importorskip("psycopg")

    from ai_nature_company.migration_readiness import assess_database_migrations

    assert DATABASE_URL is not None
    migrations = [path.read_text() for path in sorted(Path("supabase/migrations").glob("*.sql"))]
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as connection:
        for migration in migrations:
            await connection.execute(migration)
        await connection.execute("SET ROLE ai_nature_company_runtime")
        try:
            role = await (await connection.execute("SELECT current_user")).fetchone()
            assert role == (RUNTIME_ROLE,)
            report = await assess_database_migrations(connection)
        finally:
            await connection.execute("RESET ROLE")

    assert report.ready is True
    assert report.applied_names_readable is False
    assert report.missing_migrations == ()
    assert report.missing_schema_facts == ()
    assert report.forbidden_schema_facts_present == ()
    assert "active_reservation_partial_spend" in report.shipped_slugs
    assert "agent_run_expiry_diagnostics" in report.shipped_slugs


async def test_runtime_denied_migration_ledger_does_not_abort_readiness_transaction() -> None:
    """Railway pool connections are not autocommit; a denied ledger SELECT must not 503 /ready."""

    psycopg = pytest.importorskip("psycopg")
    from psycopg.errors import InsufficientPrivilege, UndefinedTable

    from ai_nature_company.migration_readiness import assess_database_migrations

    assert DATABASE_URL is not None
    migrations = [path.read_text() for path in sorted(Path("supabase/migrations").glob("*.sql"))]
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as admin:
        for migration in migrations:
            await admin.execute(migration)
        await admin.execute("CREATE SCHEMA IF NOT EXISTS supabase_migrations")
        await admin.execute(
            """
            CREATE TABLE IF NOT EXISTS supabase_migrations.schema_migrations (
                version text PRIMARY KEY,
                name text
            )
            """
        )
        await admin.execute("REVOKE ALL ON SCHEMA supabase_migrations FROM PUBLIC")
        await admin.execute(
            "REVOKE ALL ON SCHEMA supabase_migrations FROM ai_nature_company_runtime"
        )
        await admin.execute(
            "REVOKE ALL ON SCHEMA supabase_migrations FROM ai_nature_company_operator"
        )
        await admin.execute("REVOKE ALL ON ALL TABLES IN SCHEMA supabase_migrations FROM PUBLIC")
        await admin.execute(
            """
            REVOKE ALL ON ALL TABLES IN SCHEMA supabase_migrations
            FROM ai_nature_company_runtime
            """
        )
        await admin.execute(
            """
            REVOKE ALL ON ALL TABLES IN SCHEMA supabase_migrations
            FROM ai_nature_company_operator
            """
        )

    try:
        async with await psycopg.AsyncConnection.connect(
            DATABASE_URL,
            autocommit=False,
        ) as connection:
            assert connection.autocommit is False
            await connection.execute("SET ROLE ai_nature_company_runtime")
            role = await (await connection.execute("SELECT current_user")).fetchone()
            assert role == (RUNTIME_ROLE,)

            catalog = await (
                await connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM pg_catalog.pg_constraint
                    WHERE conname = %s
                    """,
                    ("anc_budget_reservations_active_partial_spend_check",),
                )
            ).fetchone()
            assert catalog is not None and catalog[0] >= 1

            with pytest.raises((InsufficientPrivilege, UndefinedTable)):
                await connection.execute("SELECT name FROM supabase_migrations.schema_migrations")
            await connection.rollback()

            await connection.execute("SET ROLE ai_nature_company_runtime")
            report = await assess_database_migrations(connection)
            still_runtime = await (await connection.execute("SELECT current_user")).fetchone()
            assert still_runtime == (RUNTIME_ROLE,)
    finally:
        async with await psycopg.AsyncConnection.connect(
            DATABASE_URL,
            autocommit=True,
        ) as admin:
            await admin.execute("DROP SCHEMA IF EXISTS supabase_migrations CASCADE")

    assert report.ready is True
    assert report.applied_names_readable is False
    assert report.missing_migrations == ()


async def test_runtime_role_pages_running_runs_and_kill_ends_only_that_cells_runs() -> None:
    """maintenance.run pages every running run; cell kill ends one cell's runs in-tx."""

    psycopg = pytest.importorskip("psycopg")

    from ai_nature_company.postgres import _PostgresTransaction

    assert DATABASE_URL is not None
    migrations = [path.read_text() for path in sorted(Path("supabase/migrations").glob("*.sql"))]
    run_company = f"kill-runs-{uuid4().hex[:8]}"
    now = datetime.now(UTC)

    def cell_for(index: int) -> str:
        return "cell-1" if index < 450 else "cell-2"

    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as connection:
        for migration in migrations:
            await connection.execute(migration)
        # Synthetic rows only: skip FK/identity triggers so the test isolates paging.
        await connection.execute("SET session_replication_role = replica")
        try:
            for index in range(453):
                instance_id = f"{run_company}-inst-{index:04d}"
                await connection.execute(
                    """
                    INSERT INTO public.anc_agent_instances
                        (company_id, agent_instance_id, principal_id, definition_id,
                         definition_version, role, cell_id, status, revision,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, 'specialist', 1, 'specialist', %s,
                            'active', 0, now(), now())
                    """,
                    (
                        run_company,
                        instance_id,
                        f"{run_company}-principal-{index:04d}",
                        cell_for(index),
                    ),
                )
                await connection.execute(
                    """
                    INSERT INTO public.anc_agent_runs
                        (company_id, agent_run_id, agent_instance_id, principal_id,
                         definition_id, definition_version, role, cell_id, status,
                         termination_code, revision, started_at, expires_at, ended_at,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, %s, 'specialist', 1, 'specialist', %s,
                            'running', NULL, 0, %s, %s, NULL, now(), now())
                    """,
                    (
                        run_company,
                        f"{run_company}-run-{index:04d}",
                        instance_id,
                        f"{run_company}-principal-{index:04d}",
                        cell_for(index),
                        now - timedelta(hours=1),
                        # Groups of seven share an expiry to exercise the id tiebreak.
                        now + timedelta(minutes=index // 7),
                    ),
                )
        finally:
            await connection.execute("SET session_replication_role = origin")

        await connection.execute("SET ROLE ai_nature_company_runtime")
        try:
            tx = _PostgresTransaction(connection, run_company)
            runs = await scan_running_agent_runs(tx)
            capped = await scan_running_agent_runs(tx, cap=300)
            ended = await tx.end_cell_agent_runs(
                cell_id="cell-1", termination_code="cell_killed", ended_at=now
            )
            again = await tx.end_cell_agent_runs(
                cell_id="cell-1", termination_code="cell_killed", ended_at=now
            )
            remaining = await scan_running_agent_runs(tx)
        finally:
            await connection.execute("RESET ROLE")
        cursor = await connection.execute(
            """
            SELECT count(*) FILTER (WHERE status = 'revoked' AND termination_code = 'cell_killed'
                                    AND ended_at IS NOT NULL AND revision = 1),
                   count(*) FILTER (WHERE status = 'running')
            FROM public.anc_agent_runs WHERE company_id = %s
            """,
            (run_company,),
        )
        counts = await cursor.fetchone()

    assert [run.agent_run_id for run in runs.runs] == [
        f"{run_company}-run-{index:04d}" for index in range(453)
    ]
    assert runs.truncated is False
    assert capped.truncated is True
    assert len(capped.runs) == 300
    assert ended == sorted(f"{run_company}-run-{index:04d}" for index in range(450))
    assert again == []
    assert [run.cell_id for run in remaining.runs] == ["cell-2"] * 3
    assert counts == (450, 3)
