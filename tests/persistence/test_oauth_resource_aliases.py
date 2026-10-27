from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

ALIAS_MIGRATION = Path("supabase/migrations/202608310007_oauth_client_resource_aliases.sql")
PATH_ALIAS_MIGRATION = Path("supabase/migrations/202608310009_oauth_path_resource_aliases.sql")
DATABASE_URL = os.getenv("ANC_TEST_DATABASE_URL")
PRIMARY_RESOURCE = "https://substrate.example.test/mcp"
ALIAS_RESOURCE = "https://alias-host.example.test/mcp"
PATH_ALIAS_RESOURCE = "https://substrate.example.test/mcp/operating-specialist-1"
UNKNOWN_RESOURCE = "https://unknown-host.example.test/mcp"
UNKNOWN_PATH_RESOURCE = "https://substrate.example.test/mcp/unknown-slug-1"


def test_alias_migration_is_additive_and_keeps_exact_resource_match() -> None:
    sql = ALIAS_MIGRATION.read_text(encoding="utf-8")
    assert "202608300003" not in sql
    assert "CREATE TABLE IF NOT EXISTS public.anc_oauth_client_resource_aliases" in sql
    assert "PRIMARY KEY (company_id, client_id, resource_url)" in sql
    assert "resource_url ~ '^https://[^?#]+/mcp$'" in sql
    assert "allowed_client.resource_url = $2" in sql
    assert "allowed_alias.resource_url = $2" in sql
    assert "allowed_alias.status = 'active'" in sql
    assert "allowed_client.status = 'active'" in sql
    assert "run.started_at <= now()" in sql
    assert "run.expires_at > now()" in sql
    assert "session.oauth_client_id = upstream_client.id" in sql
    assert "refresh.revoked = false" in sql
    assert "anc_oauth_client_resource_aliases_identity_guard" in sql
    assert "OAuth client resource alias identity is immutable" in sql
    assert "REVOKE ALL ON TABLE public.anc_oauth_client_resource_aliases" in sql
    assert "FROM ai_nature_company_runtime" in sql
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE public.anc_oauth_client_resource_aliases" in sql
    assert "GRANT DELETE" not in sql
    assert "CREATE OR REPLACE FUNCTION public.anc_resolve_agent_identity" in sql


def test_path_alias_migration_is_additive_and_keeps_exact_resource_match() -> None:
    sql = PATH_ALIAS_MIGRATION.read_text(encoding="utf-8")
    assert "202608310007" not in sql
    assert "DROP CONSTRAINT IF EXISTS anc_oauth_client_resource_aliases_resource_url_check" in sql
    assert "ADD CONSTRAINT anc_oauth_client_resource_aliases_resource_url_check" in sql
    assert "resource_url ~ '^https://[^?#]+/mcp(?:/[a-z0-9]+(?:-[a-z0-9]+)*)?$'" in sql
    assert "CREATE OR REPLACE FUNCTION" not in sql
    assert "DROP TABLE" not in sql
    assert "ALTER TABLE public.anc_oauth_clients" not in sql
    assert "GRANT DELETE" not in sql


@pytest.mark.skipif(
    not DATABASE_URL,
    reason="set ANC_TEST_DATABASE_URL to run live PostgreSQL proofs",
)
async def test_resolver_accepts_listed_aliases_and_rejects_unlisted_resources() -> None:
    psycopg = pytest.importorskip("psycopg")
    assert DATABASE_URL is not None
    oauth_client_id = uuid4()
    public_client_id = str(oauth_client_id)
    other_client_id = str(uuid4())
    subject_id = uuid4()
    session_id = uuid4()
    suffix = uuid4().hex
    company_id = f"alias-company-{suffix}"
    auth_subject_id = f"alias-subject-{suffix}"
    principal_id = f"alias-principal-{suffix}"
    instance_id = f"alias-instance-{suffix}"
    run_id = f"alias-run-{suffix}"
    issuer = "https://project-ref.supabase.co/auth/v1"
    created_at = datetime.now(UTC) - timedelta(minutes=2)
    token_valid_after = created_at + timedelta(seconds=30)
    session_created_at = created_at + timedelta(minutes=1)
    run_expires_at = created_at + timedelta(hours=1)

    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as connection:
        cursor = await connection.execute("SELECT to_regnamespace('auth') IS NOT NULL")
        row = await cursor.fetchone()
        assert row is not None
        if row[0]:
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
            migration_paths = sorted(Path("supabase/migrations").glob("*.sql"))
            assert ALIAS_MIGRATION in migration_paths
            alias_index = migration_paths.index(ALIAS_MIGRATION)
            for path in migration_paths[:alias_index]:
                await connection.execute(path.read_text())

            await connection.execute(
                "INSERT INTO auth.oauth_clients (id, deleted_at) VALUES (%s, NULL)",
                (oauth_client_id,),
            )
            await connection.execute(
                """
                INSERT INTO auth.sessions
                    (id, user_id, oauth_client_id, not_after, created_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (session_id, subject_id, oauth_client_id, run_expires_at, session_created_at),
            )
            await connection.execute(
                "INSERT INTO auth.refresh_tokens (session_id, revoked) VALUES (%s, false)",
                (session_id,),
            )
            await connection.execute(f"SELECT set_config('app.company_id', '{company_id}', false)")
            await connection.execute(
                """
                INSERT INTO public.anc_oauth_clients
                    (client_id, company_id, resource_url, display_name, status,
                     revision, created_at, updated_at)
                VALUES (%s, %s, %s, 'Alias integration client', 'active',
                        0, %s, %s)
                """,
                (public_client_id, company_id, PRIMARY_RESOURCE, created_at, created_at),
            )
            await connection.execute(ALIAS_MIGRATION.read_text())
            for path in migration_paths[alias_index + 1 :]:
                await connection.execute(path.read_text())
            backfill = await connection.execute(
                """
                SELECT resource_url, status
                FROM public.anc_oauth_client_resource_aliases
                WHERE company_id = %s AND client_id = %s
                """,
                (company_id, public_client_id),
            )
            assert await backfill.fetchall() == [(PRIMARY_RESOURCE, "active")]

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

            async def resolve(resource_url: str, *, client_id: str = public_client_id) -> int:
                resolver_cursor = await connection.execute(
                    """
                    SELECT principal_id
                    FROM public.anc_resolve_agent_identity(%s, %s, %s, %s, %s)
                    """,
                    (issuer, str(subject_id), client_id, resource_url, str(session_id)),
                )
                return len(await resolver_cursor.fetchall())

            await connection.execute("SET ROLE ai_nature_company_runtime")
            assert await resolve(PRIMARY_RESOURCE) == 1
            assert await resolve(UNKNOWN_RESOURCE) == 0
            await connection.execute("RESET ROLE")

            await connection.execute(
                """
                INSERT INTO public.anc_oauth_client_resource_aliases
                    (company_id, client_id, resource_url, status, revision,
                     created_at, updated_at)
                VALUES (%s, %s, %s, 'active', 0, %s, %s)
                """,
                (company_id, public_client_id, ALIAS_RESOURCE, created_at, created_at),
            )
            await connection.execute(
                """
                INSERT INTO public.anc_oauth_clients
                    (client_id, company_id, resource_url, display_name, status,
                     revision, created_at, updated_at)
                VALUES (%s, %s, %s, 'Other client', 'active', 0, %s, %s)
                """,
                (other_client_id, company_id, UNKNOWN_RESOURCE, created_at, created_at),
            )

            await connection.execute(
                """
                INSERT INTO public.anc_oauth_client_resource_aliases
                    (company_id, client_id, resource_url, status, revision,
                     created_at, updated_at)
                VALUES (%s, %s, %s, 'active', 0, %s, %s)
                """,
                (company_id, public_client_id, PATH_ALIAS_RESOURCE, created_at, created_at),
            )

            await connection.execute("SET ROLE ai_nature_company_runtime")
            try:
                assert await resolve(PRIMARY_RESOURCE) == 1
                assert await resolve(ALIAS_RESOURCE) == 1
                assert await resolve(PATH_ALIAS_RESOURCE) == 1
                assert await resolve(UNKNOWN_RESOURCE) == 0
                assert await resolve(UNKNOWN_PATH_RESOURCE) == 0
                assert await resolve(ALIAS_RESOURCE, client_id=other_client_id) == 0
                assert await resolve(PATH_ALIAS_RESOURCE, client_id=other_client_id) == 0
            finally:
                await connection.execute("RESET ROLE")

            await connection.execute(
                """
                UPDATE public.anc_oauth_client_resource_aliases
                SET status = 'disabled',
                    revision = revision + 1,
                    updated_at = %s
                WHERE company_id = %s
                  AND client_id = %s
                  AND resource_url = %s
                """,
                (created_at + timedelta(seconds=1), company_id, public_client_id, ALIAS_RESOURCE),
            )
            with pytest.raises(Exception, match="immutable"):
                await connection.execute(
                    """
                    UPDATE public.anc_oauth_client_resource_aliases
                    SET resource_url = %s
                    WHERE company_id = %s
                      AND client_id = %s
                      AND resource_url = %s
                    """,
                    (
                        "https://rewritten-host.example.test/mcp",
                        company_id,
                        public_client_id,
                        ALIAS_RESOURCE,
                    ),
                )
            with pytest.raises(Exception, match="DELETE is forbidden"):
                await connection.execute(
                    """
                    DELETE FROM public.anc_oauth_client_resource_aliases
                    WHERE company_id = %s AND client_id = %s AND resource_url = %s
                    """,
                    (company_id, public_client_id, ALIAS_RESOURCE),
                )

            await connection.execute("SET ROLE ai_nature_company_runtime")
            try:
                assert await resolve(PRIMARY_RESOURCE) == 1
                assert await resolve(ALIAS_RESOURCE) == 0
                assert await resolve(PATH_ALIAS_RESOURCE) == 1
                assert await resolve(UNKNOWN_RESOURCE) == 0
            finally:
                await connection.execute("RESET ROLE")
        finally:
            await connection.execute("DROP SCHEMA auth CASCADE")
