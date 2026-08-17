---
sidebar_position: 18
title: "PostgreSQL State Backend"
description: "Optional PostgreSQL backend for session and state storage, in place of the default single-file SQLite database"
---

# PostgreSQL State Backend

Hermes stores sessions, messages, and agent state in a single SQLite file
(`~/.hermes/state.db`) by default. That is the right choice for a personal
install: zero setup, zero moving parts, and it comfortably handles tens of
thousands of messages.

Some deployments outgrow it. If you run Hermes across several hosts against
shared state, on a container filesystem where SQLite's locking semantics do not
hold, or under an operational policy that requires a managed database, you can
point session storage at an external PostgreSQL server instead.

This backend is **opt-in and off by default**. Installs that do not configure it
never load the driver and never pay for it.

## When to use it

Consider PostgreSQL when any of these apply:

- **Multiple hosts share one state store.** Several Hermes processes (gateway,
  dashboard, cron) running on different machines against the same sessions.
- **The filesystem is unsuitable for SQLite.** Network filesystems and some
  container volumes do not provide the locking or `mmap` guarantees SQLite needs.
- **Operational policy requires a managed database.** Backups, point-in-time
  restore, failover, and monitoring handled by existing database infrastructure.

Stay on the default SQLite backend otherwise. It is faster for a single-host
install, needs no server, and is the more thoroughly exercised path.

## Requirements

- **PostgreSQL 14 or newer.**
- **The `postgres` extra**, which installs the `psycopg` driver:

  ```bash
  pip install 'hermes-agent[postgres]'
  # or, from a source checkout:
  uv sync --extra postgres
  ```

  Hermes will also attempt to install the driver on first use if it is missing,
  so an existing install that flips the config key generally does not need a
  manual step. Environments that block outbound PyPI access should install the
  extra ahead of time. The published Docker image ships with it already baked in.

- **The `pg_trgm` extension** is recommended but not required. Hermes tries to
  create it on first connect (`CREATE EXTENSION IF NOT EXISTS pg_trgm`); it
  backs the GIN trigram indexes that accelerate substring search.

  If the connecting role may not create extensions, Hermes logs a warning and
  continues — search still works, because the `ILIKE` path is plain SQL and the
  full-text path uses core PostgreSQL `tsvector`. Only the trigram acceleration
  is lost, which matters once the message table is large. Hermes re-attempts the
  extension on every connect, so enabling it later is picked up automatically
  with no migration to run by hand.

  On self-managed PostgreSQL, a superuser can install it once per database:
  `CREATE EXTENSION IF NOT EXISTS pg_trgm;`

  See [Enabling `pg_trgm` on managed PostgreSQL](#enabling-pg_trgm-on-managed-postgresql)
  below when the provider restricts extensions.

The database user needs `CREATE` on the target database — Hermes manages its own
tables, indexes, and migrations.

### Enabling `pg_trgm` on managed PostgreSQL

Hosted providers only let non-superusers create extensions from an allow-list.
Hermes runs fine without `pg_trgm`, but enabling it is recommended before the
message table grows, because it is what keeps substring search off a sequential
scan.

**Azure Database for PostgreSQL — Flexible Server.** The `azure.extensions`
server parameter is empty by default, and `CREATE EXTENSION` fails with:

```
ERROR: extension "pg_trgm" is not allow-listed for users in
       Azure Database for PostgreSQL
```

Add it to the allow-list, preserving any extensions already listed — the value
is a comma-separated list and setting it replaces the whole thing:

```bash
# Inspect the current value first so you don't drop existing entries.
az postgres flexible-server parameter show \
  --resource-group <rg> --server-name <server> \
  --name azure.extensions --query value -o tsv

az postgres flexible-server parameter set \
  --resource-group <rg> --server-name <server> \
  --name azure.extensions --value pg_trgm
```

In the portal the parameter is under **Server parameters**; search for
`azure.extensions` and pick `pg_trgm` from the dropdown rather than the "All"
tab, where it is not listed.

`azure.extensions` is a dynamic parameter, so the change generally applies
without a restart — confirm with:

```bash
az postgres flexible-server parameter show \
  --resource-group <rg> --server-name <server> \
  --name azure.extensions \
  --query "{value:value,dynamic:isDynamicConfig,restartPending:isConfigPendingRestart}"
```

If `restartPending` is true, or `CREATE EXTENSION` still reports the extension
as not allow-listed, restart the server and retry:

```bash
az postgres flexible-server restart --resource-group <rg> --name <server>
```

Allow-listing makes the extension *available*; it does not install it.
Extensions are per-database, so each database needs its own `CREATE EXTENSION`.
Hermes does this itself on the next connect — restart the agent, or just let it
reconnect, and it will install `pg_trgm` and build the trigram indexes with no
further action. To confirm:

```sql
SELECT count(*) FROM pg_extension WHERE extname = 'pg_trgm';   -- expect 1
SELECT count(*) FROM pg_indexes WHERE indexname LIKE '%\_trgm';  -- expect 3
```

**Other managed providers.** Amazon RDS and Aurora ship `pg_trgm` in the
default `rds.allowed_extensions`, so `CREATE EXTENSION` normally just works.
Google Cloud SQL supports it without an allow-list. In both cases the role
still needs sufficient privilege on the target database.

## Enabling it

Two settings, both under `sessions:` in `~/.hermes/config.yaml`:

```yaml
sessions:
  state_backend: postgres
  postgres_dsn: "postgresql://hermes:secret@db.example.com:5432/hermes?sslmode=require"
```

Or via the CLI:

```bash
hermes config set sessions.state_backend postgres
hermes config set sessions.postgres_dsn 'postgresql://...'
```

The DSN is passed to the driver **unchanged**, so TLS mode, host, port, and
credentials are entirely yours to specify. Use `sslmode=require` (or stricter)
for anything crossing a network.

### Environment variables

All three take precedence over the corresponding `config.yaml` keys, which is
convenient for containers and CI:

| Variable | Purpose |
|---|---|
| `HERMES_STATE_DATABASE_URL` | PostgreSQL DSN |
| `HERMES_STATE_POSTGRES_DSN` | Alternate name for the same value |
| `HERMES_STATE_BACKEND` | `sqlite` (default) or `postgres` |

Resolution order for the DSN is: `HERMES_STATE_DATABASE_URL` →
`HERMES_STATE_POSTGRES_DSN` → `sessions.postgres_dsn`. For backend selection it
is `HERMES_STATE_BACKEND` → `sessions.state_backend`.

Because the DSN carries credentials, it belongs in `~/.hermes/.env` (or your
orchestrator's secret store) rather than in `config.yaml` when you use the
environment-variable form.

## Migrating existing sessions

The `hermes migrate state-to-postgres` subcommand performs a one-shot copy of
an existing SQLite state database into PostgreSQL:

```bash
hermes migrate state-to-postgres [--dsn 'postgresql://...'] [--sqlite-path PATH]
```

Running without `--dsn` resolves the target from the
`HERMES_STATE_DATABASE_URL` / `HERMES_STATE_POSTGRES_DSN` environment
variables, or from `sessions.postgres_dsn` in `config.yaml` when
`sessions.state_backend: postgres` is already set. `--sqlite-path` defaults
to `state.db` under your Hermes home.

Use `-y` / `--yes` to skip the confirmation prompt in scripts or CI:

```bash
hermes migrate state-to-postgres --dsn 'postgresql://...' --yes
```

The equivalent direct invocation (documented for scripting or for users who
prefer to run the module without the CLI) is:

```bash
python -m migrate_state_to_postgres --dsn 'postgresql://...' [--sqlite-path PATH]
```

Its properties, by design:

- **Source-safe.** The SQLite database is opened read-only and is never mutated
  or deleted. It stays the fallback-of-record until you have verified the copy
  and flipped `sessions.state_backend`.
- **Idempotent.** Rows are inserted with `ON CONFLICT DO NOTHING`, so re-running
  after a partial run fills the gaps without duplicating. Note that it does not
  *refresh* rows already present — it targets a fresh database. If a source row
  changed after a prior partial import, drop the target and re-run.
- **Full fidelity.** Rewound (soft-deleted) messages are included, message ids
  and timestamps are preserved, and content is re-encoded through the live
  encoding path.

The script verifies session and message counts after import and reports them.

Recommended sequence:

1. Run the migration while Hermes is stopped.
2. The command reports how many sessions and messages it migrated out of the
   source total (for example `Sessions: 42/42`). Confirm both numbers match.
   These counts are scoped to the rows this run actually copied, not to the
   target's table totals — a target that already holds rows would satisfy any
   "total >= source" comparison no matter how much was dropped.
3. Set `sessions.state_backend: postgres`.
4. Start Hermes and confirm `/resume` and `session_search` behave.
5. Keep the SQLite file until you are satisfied.

## Behavioral notes

**Failure is loud, not silent.** If the DSN is wrong or the server is
unreachable, opening the session store raises. Hermes does **not** quietly fall
back to SQLite — a silent fallback would write to a different database than the
one you configured and split your history across two stores.

**Search uses native full-text indexing, with an `ILIKE` fallback.** SQLite's
FTS5 index has no direct PostgreSQL equivalent, so the backend builds its own:
a `tsvector` column (`messages.fts_content`) with a GIN index, populated for
every message as it is written. Queries use `fts_content @@ tsquery` with the
`simple` dictionary (lowercasing, no stemming — the right choice for a corpus
mixing code identifiers, proper nouns, and multiple languages), which gives
tokenized multi-word AND search.

:::note If you migrated from SQLite, read this first

Migrated rows are written before the full-text column is populated, so a
database carrying pre-existing history starts with `fts_content IS NULL` on
every imported row. Until you run the one-time backfill below, **every search
uses the `ILIKE` fallback, not full-text search.** Nothing is broken and no
result is missing — but multi-word queries behave as substring matches until
the backfill completes.

:::

Rows written *before* the full-text column existed have `fts_content IS NULL`.
Until every such row is backfilled, search deliberately stays on a
trigram-indexed `ILIKE` scan, because an FTS-only query would silently miss
every un-backfilled row — `ILIKE` covers all rows, so it is the correct choice
during that window. The `pg_trgm` GIN indexes keep it usable.

To finish the transition on a database with pre-existing history, run the
one-time backfill (safe to run in batches, and safe to re-run):

```sql
UPDATE messages
   SET fts_content = to_tsvector('simple', coalesce(content, ''))
 WHERE fts_content IS NULL;
```

Search switches to the FTS path automatically once no `NULL` rows remain; there
is no flag to flip. A fresh database that starts on PostgreSQL never needs this.

**Two independent schema version counters.** `SCHEMA_VERSION` governs the shared
and SQLite schema and is recorded in the `schema_version` table. The PostgreSQL
backend keeps its own migration list with its own counter, recorded separately
in `pg_migration_version`. The two numbers are deliberately unrelated — do not
expect them to match, and do not merge the tables. (They shared one table
originally, which meant that once the shared version climbed past the highest
Postgres-only migration number, every Postgres migration looked already-applied
and was skipped.)

Every Postgres-only migration statement is `IF NOT EXISTS`-guarded, so a
database with no `pg_migration_version` row simply replays the list; existing
objects are left alone and anything missing is created.

**Read-only cross-profile attach stays on SQLite.** The read-only path used to
poll another profile's database is SQLite-specific and is not served by this
backend.

**Structured message content uses a `U+0001` sentinel prefix.** PostgreSQL's
`text` type cannot store `NUL`, so the marker distinguishing JSON-encoded
multimodal content from plain strings uses `U+0001` on write. The legacy
`NUL` prefix is still accepted on read, so rows written by older versions decode
correctly on both backends.

## Verifying

After enabling, confirm the backend is actually engaged:

```bash
# Sessions should appear in the target database, not in state.db
psql "$HERMES_STATE_DATABASE_URL" -c 'SELECT count(*) FROM sessions;'
psql "$HERMES_STATE_DATABASE_URL" -c 'SELECT count(*) FROM messages;'
```

Then start a session, send a message, and re-run the message count — it should
increase. A count that stays flat while Hermes appears healthy means the backend
did not engage and writes are still going to SQLite; check that
`sessions.state_backend` is `postgres` and that no stale `HERMES_STATE_BACKEND`
is overriding it.

## Troubleshooting

**`PostgreSQL state backend requires psycopg`** — the `postgres` extra is not
installed and the automatic install did not succeed. Install it explicitly:
`pip install 'hermes-agent[postgres]'`.

**`permission denied to create extension "pg_trgm"`** or **`extension "pg_trgm"
is not allow-listed`** — the connecting role cannot create extensions. This is
**not fatal**: Hermes logs a warning, skips the extension and its trigram
indexes, and runs with search intact but unaccelerated. Fix it at your leisure —
on self-managed PostgreSQL have a superuser run
`CREATE EXTENSION IF NOT EXISTS pg_trgm;` against the target database; on a
managed provider see
[Enabling `pg_trgm` on managed PostgreSQL](#enabling-pg_trgm-on-managed-postgresql).
Hermes retries on the next connect and installs it once permitted.

**Search feels slow on a large database** — check whether the trigram indexes
exist (`SELECT count(*) FROM pg_indexes WHERE indexname LIKE '%\_trgm';`,
expect 3). Zero means `pg_trgm` was never installed, so substring search is
sequential-scanning. Enable the extension as above.

**Turn errors mentioning connection failures** — a managed server restarting for
maintenance surfaces as transient contention and is reported as a retryable
condition rather than storage damage. Retry the message; if it persists, check
server availability and connection limits.

**Search returns nothing** — confirm the backend engaged (see *Verifying*
above). Zero results with a healthy-looking gateway is the classic symptom of
writes landing in one store while reads come from another.
