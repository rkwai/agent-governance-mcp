# Deploy contract: shipped migrations must be applied before `/ready`

A production deploy must not look healthy while the SQL the image ships has not
been applied to the live database. Railway starts the MCP process only. It does
not run `supabase/migrations`. Those files are applied out of band — historically
by hand through Supabase `apply_migration` into
`supabase_migrations.schema_migrations`.

PR #26 shipped `202609270001_active_reservation_partial_spend.sql`. The Railway
image came up, `/ready` returned 200, and the first live `experiment.record_send`
then failed on `anc_budget_reservations_check2` because that migration had never
been applied.

## Health probes

| Path | Meaning | Railway |
|---|---|---|
| `GET /health` | Process liveness. Always `{"status":"ok"}` once the listener is up. No dependency check. | Not the deploy healthcheck. |
| `GET /ready` | Dependency readiness. Railway `healthcheckPath`. | `railway.toml` already sets `healthcheckPath = "/ready"`. |

`/ready` returns 200 `{"status":"ready"}` only when all of the following hold:

1. Hosted privilege preflight passed (runtime role, RLS, grants, auth catalog function).
2. `public.anc_auth_catalog_compatible()` is true on this probe.
3. Every shipped migration is present on the live database.

Otherwise `/ready` is 503. When migrations are the reason, the body lists them:

```json
{
  "status": "unavailable",
  "reason": "database_migrations_unapplied",
  "message": "database migrations unapplied: missing_migrations=[active_reservation_partial_spend]; ...",
  "missing_migrations": ["active_reservation_partial_spend"],
  "missing_schema_facts": [
    "constraint public.anc_budget_reservations.anc_budget_reservations_active_partial_spend_check"
  ],
  "forbidden_schema_facts_present": [
    "constraint public.anc_budget_reservations.anc_budget_reservations_check2"
  ]
}
```

`/health` stays 200 so the process can report that list. Railway must keep
probing `/ready`; a 200 `/health` is not permission to take traffic.

No Railway variable or `railway.toml` change is required for this gate.
`healthcheckTimeout = 30` is enough. Do not point `healthcheckPath` at `/health`.

## How readiness decides a migration is applied

Repository filenames use a local prefix (`202609270001_…`). Supabase assigns its
own `version` (`20260831040448` … `20260927041044`). **Do not compare versions.**

1. **Slug / name.** Strip `^\d+_` from the shipped filename stem.
   `202609270001_active_reservation_partial_spend.sql` matches ledger `name`
   `active_reservation_partial_spend` (or `*_active_reservation_partial_spend`).
   Live `ai_nature_company_runtime` and `ai_nature_company_operator` have no
   `USAGE` on `supabase_migrations` and no `SELECT` on `schema_migrations`, so
   production `applied_names` is always `None`. The probe checks
   `has_schema_privilege` / `has_table_privilege` first and, on the Railway
   pool's non-autocommit connection, wraps any follow-up read in a savepoint.
   A denied `SELECT` must not abort the readiness transaction.
2. **Schema fingerprint.** Required tables, columns, constraints, functions, and
   roles are read from `pg_catalog` **as the runtime role** (the Railway
   `DATABASE_URL` login). That catalog is readable without extra grants. This
   is the fail-closed production gate. The #26 incident is encoded explicitly:
   `anc_budget_reservations_active_partial_spend_check` must exist and
   `anc_budget_reservations_check2` must not.

A name recorded in the ledger cannot hide a missing constraint. A complete
fingerprint cannot hide a readable ledger that omits a shipped slug.

The image copies `supabase/migrations` and also packages those files as
`ai_nature_company/shipped_migrations`. Override the search path only in
exceptional diagnostics with `ANC_MIGRATIONS_DIR`.

## Apply migrations (operator window, not Railway)

Do not put a database-owner, Supabase service-role, or operator DSN on the
Railway service. The runtime role stays least-privilege and must not apply DDL.

Apply each unapplied file from merged `main` in lexicographic order during a
bounded operator window (Supabase `apply_migration` or an approved migration
connection). Use the **slug** as the provider `name`, not the file's numeric
prefix:

```text
name: active_reservation_partial_spend
file: supabase/migrations/202609270001_active_reservation_partial_spend.sql
```

Record the filename, slug, and SHA-256. Confirm
`supabase_migrations.schema_migrations.name` contains every shipped slug, then
confirm `/ready` is 200 on each Railway service before calling the deploy
healthy.

This repository does not auto-apply migrations from the runtime process and
does not add a Railway pre-deploy step that would require an admin secret in
the service.

## Adding a migration

1. Add a new additive `supabase/migrations/<digits>_<slug>.sql`. Never edit a
   published file.
2. Add at least one `REQUIRED_SCHEMA_FACTS` entry for that slug in
   `src/ai_nature_company/migration_readiness.py` so `/ready` can detect a
   skipped apply without reading the provider ledger. CI runs
   `test_every_repo_migration_file_has_a_schema_fact`: a new
   `supabase/migrations/*.sql` file with no matching slug fails the build. The
   fingerprint is the only production gate, so a file without a fact is a
   silent hole.
3. If the migration replaces a constraint, record both the new name (required)
   and the retired name (`required=False`).
4. Cover the fact with a unit test. The hosted `/ready` 503 body must name the
   slug.
5. Apply the SQL to the live project **before** or **with** the Railway deploy,
   never after traffic is declared healthy.
