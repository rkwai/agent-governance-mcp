from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

HARDENING = Path("supabase/migrations/202608310006_closed_beta_authority_hardening.sql")
DATABASE_URL = os.getenv("ANC_TEST_DATABASE_URL")


def test_hardening_migration_is_additive_and_closes_authority_gaps() -> None:
    sql = HARDENING.read_text(encoding="utf-8")
    assert "202608300003" not in sql
    assert "CREATE OR REPLACE FUNCTION public.anc_resolve_agent_identity" in sql
    assert "run.started_at <= now()" in sql
    assert "run.expires_at > now()" in sql
    assert (
        "REVOKE ALL ON FUNCTION public.anc_validate_mcp_tool_call_attribution() FROM PUBLIC"
    ) in sql
    assert "NEW.role, NEW.cell_id" in sql
    assert "received_record.role, received_record.cell_id" in sql
    assert "an active agent instance cannot be created for a disabled or revoked subject" in sql
    assert "a running agent run cannot be created for a disabled or revoked subject" in sql
    assert "anc_agent_instances_active_cell_owner_requires_cell" in sql
    assert "rolreplication" in sql
    assert "pg_catalog.pg_auth_members" in sql
    assert "has_schema_privilege('ai_nature_company_runtime', 'public', 'CREATE')" in sql


@pytest.mark.skipif(
    not DATABASE_URL,
    reason="set ANC_TEST_DATABASE_URL to run live PostgreSQL proofs",
)
async def test_hardening_invariants_and_acls_on_disposable_postgres() -> None:
    psycopg = pytest.importorskip("psycopg")
    assert DATABASE_URL is not None
    migrations = [path.read_text() for path in sorted(Path("supabase/migrations").glob("*.sql"))]
    suffix = uuid4().hex
    company_id = f"harden-company-{suffix}"
    now = datetime.now(UTC)
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as connection:
        for migration in migrations:
            await connection.execute(migration)

        acl = await connection.execute(
            """
            SELECT COALESCE(
                (
                    SELECT bool_or(acl.grantee = 0 AND acl.privilege_type = 'EXECUTE')
                    FROM aclexplode(
                        COALESCE(procedure.proacl, acldefault('f', procedure.proowner))
                    ) AS acl
                ),
                false
            )
            FROM pg_catalog.pg_proc AS procedure
            WHERE procedure.oid = to_regprocedure(
                'public.anc_validate_mcp_tool_call_attribution()'
            )
            """
        )
        assert await acl.fetchone() == (False,)

        untrusted = f"anc_untrusted_{suffix}"
        await connection.execute(
            f"CREATE ROLE {untrusted} NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOLOGIN NOBYPASSRLS"
        )
        try:
            execute_priv = await connection.execute(
                """
                SELECT has_function_privilege(
                    %s,
                    'public.anc_validate_mcp_tool_call_attribution()',
                    'EXECUTE'
                )
                """,
                (untrusted,),
            )
            assert await execute_priv.fetchone() == (False,)
        finally:
            await connection.execute(f"DROP ROLE {untrusted}")

        await connection.execute(f"SELECT set_config('app.company_id', '{company_id}', false)")
        client_id = str(uuid4())
        principal_id = f"harden-principal-{suffix}"
        subject_id = f"harden-subject-{suffix}"
        await connection.execute(
            """
            INSERT INTO public.anc_oauth_clients
                (client_id, company_id, resource_url, display_name, status,
                 revision, created_at, updated_at)
            VALUES (%s, %s, 'https://substrate.example.test/mcp', 'Hardening',
                    'active', 0, %s, %s)
            """,
            (client_id, company_id, now, now),
        )
        await connection.execute(
            """
            INSERT INTO public.anc_auth_subjects
                (company_id, auth_subject_id, issuer, external_subject,
                 oauth_client_id, principal_id, subject_kind, status,
                 token_valid_after, disabled_at, revision, created_at, updated_at)
            VALUES
                (%s, %s, %s, %s, %s, %s, 'agent', 'disabled',
                 %s, %s, 0, %s, %s)
            """,
            (
                company_id,
                subject_id,
                "https://project-ref.supabase.co/auth/v1",
                str(uuid4()),
                client_id,
                principal_id,
                now,
                now,
                now,
                now,
            ),
        )
        with pytest.raises(Exception, match="disabled or revoked subject"):
            await connection.execute(
                """
                INSERT INTO public.anc_agent_instances
                    (company_id, agent_instance_id, principal_id, definition_id,
                     definition_version, role, cell_id, status, revision,
                     created_at, updated_at)
                VALUES (%s, %s, %s, 'specialist', 1, 'specialist', 'cell-1',
                        'active', 0, %s, %s)
                """,
                (company_id, f"instance-{suffix}", principal_id, now, now),
            )

        with pytest.raises(Exception, match="cell_owner"):
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
                    f"{subject_id}-owner",
                    "https://project-ref.supabase.co/auth/v1",
                    str(uuid4()),
                    client_id,
                    f"{principal_id}-owner",
                    now,
                    now,
                    now,
                ),
            )
            await connection.execute(
                """
                INSERT INTO public.anc_agent_instances
                    (company_id, agent_instance_id, principal_id, definition_id,
                     definition_version, role, cell_id, status, revision,
                     created_at, updated_at)
                VALUES (%s, %s, %s, 'owner', 1, 'cell_owner', NULL,
                        'active', 0, %s, %s)
                """,
                (company_id, f"owner-{suffix}", f"{principal_id}-owner", now, now),
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
                f"{subject_id}-ok",
                "https://project-ref.supabase.co/auth/v1",
                str(uuid4()),
                client_id,
                f"{principal_id}-ok",
                now,
                now,
                now,
            ),
        )
        await connection.execute(
            """
            INSERT INTO public.anc_agent_instances
                (company_id, agent_instance_id, principal_id, definition_id,
                 definition_version, role, cell_id, status, revision,
                 created_at, updated_at)
            VALUES (%s, %s, %s, 'specialist', 1, 'specialist', 'cell-1',
                    'active', 0, %s, %s)
            """,
            (company_id, f"ok-{suffix}", f"{principal_id}-ok", now, now),
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
                f"run-{suffix}",
                f"ok-{suffix}",
                f"{principal_id}-ok",
                now - timedelta(minutes=5),
                now + timedelta(hours=1),
                now,
                now,
            ),
        )
        with pytest.raises(Exception, match="issuance fields are immutable"):
            await connection.execute(
                """
                UPDATE public.anc_agent_runs
                SET started_at = %s
                WHERE company_id = %s AND agent_run_id = %s
                """,
                (now + timedelta(minutes=5), company_id, f"run-{suffix}"),
            )
        with pytest.raises(Exception, match="issuance fields are immutable"):
            await connection.execute(
                """
                UPDATE public.anc_agent_runs
                SET expires_at = %s
                WHERE company_id = %s AND agent_run_id = %s
                """,
                (now - timedelta(minutes=1), company_id, f"run-{suffix}"),
            )
        received_at = now
        await connection.execute(
            """
            INSERT INTO public.anc_mcp_tool_calls
                (company_id, call_id, phase, request_id, principal_id,
                 authentication_method, authentication_id, role, cell_id,
                 tool_name, transport, protocol_version, arguments_sha256,
                 authorization_outcome, execution_outcome, received_at, recorded_at)
            VALUES
                (%s, %s, 'received', %s, %s, 'local', 'local-1', 'founder', NULL,
                 'company.describe', 'stdio', '2025-03-26', repeat('a', 64),
                 'pending', 'pending', %s, %s)
            """,
            (
                company_id,
                f"call-{suffix}",
                f"req-{suffix}",
                f"{principal_id}-ok",
                received_at,
                received_at,
            ),
        )
        with pytest.raises(Exception, match="differs from its received event"):
            await connection.execute(
                """
                INSERT INTO public.anc_mcp_tool_calls
                    (company_id, call_id, phase, request_id, principal_id,
                     authentication_method, authentication_id, role, cell_id,
                     tool_name, transport, protocol_version, arguments_sha256,
                     authorization_outcome, authorization_reason_code,
                     execution_outcome, received_at, recorded_at, duration_ms)
                VALUES
                    (%s, %s, 'succeeded', %s, %s, 'local', 'local-1', 'specialist', 'cell-1',
                     'company.describe', 'stdio', '2025-03-26', repeat('a', 64),
                     'allowed', 'authorization.allowed', 'succeeded', %s, %s, 1)
                """,
                (
                    company_id,
                    f"call-{suffix}",
                    f"req-{suffix}",
                    f"{principal_id}-ok",
                    received_at,
                    received_at + timedelta(milliseconds=1),
                ),
            )
