# CloudBase candidate release

The image has two explicit, mutually exclusive profiles. The current release
bundle deliberately defaults to `ephemeral_candidate` because persistent
PostgreSQL and cross-restart recovery are deferred:

- `ephemeral_candidate` is the temporary non-production profile. It uses
  SQLite, initializes its own schema, and stores acquired snapshots on the
  container filesystem. It declares `persistence=ephemeral` and
  `restartRecoveryVerified=false`; it must not be described as durable or
  production-ready.
- `strict_production` keeps the existing fail-closed PostgreSQL/CFS entrypoint.
  It cannot start until every production prerequisite below is proven.

`deployment_entrypoint.py` still rejects a missing or unknown
`DEPLOYMENT_PROFILE`; selecting `strict_production` always uses the separate
fail-closed `runtime_entrypoint.py`. A production deployment must explicitly
override the complete strict-production environment rather than relying on the
temporary image defaults.

The current stable service stays unchanged until every gate below passes. A
candidate is created as a separate version with zero default traffic. Traffic
changes and rollback are performed in the CloudBase console because this
environment's currently verified management API is `tcb` `2018-06-08`; the
repository deliberately does not guess a different Cloud Run release API.

## Strict-production prerequisites (deferred TODO)

Do not select `strict_production` unless all of these facts have independent
evidence:

- the repository is clean and `CODE_REVISION` is its exact 40-character HEAD;
- every server-trusted Composite v2 snapshot passes the strict loader;
- PostgreSQL has an immutable pre-migration schema/role/row-count baseline and
  a valid single-connection migration/rollback rehearsal;
- Alembic is at the release head;
- a dedicated `LOGIN` runtime role exists with no `SUPERUSER`, `CREATEDB`,
  `CREATEROLE`, `REPLICATION`, or `BYPASSRLS` attribute;
- the runtime role owns no application object and has only the ACLs verified by
  `production_db.py`;
- the candidate CloudBase Run version has private VPC access to PostgreSQL;
- a provider-backed persistent snapshot filesystem is mounted at
  `/mnt/ashare-snapshots`, survives a candidate restart, and contains the
  matching durable-store marker and restart probe;
- `DATABASE_URL` is injected as a secret for that runtime role. Administrator
  URLs and passwords are never container environment variables.

Cloud PostgreSQL, CFS, and cross-restart recovery verification are explicitly
deferred. Until this TODO is completed, the strict-production state is
`production-unavailable`.

## Build an allowlisted bundle

The bundle includes application code, Alembic migrations, deployment gates,
acquisition scripts, and one or more immutable seed Composite v2 snapshots.
Repeat `--snapshot-digest` for the two release-gate instruments; the first is
the legacy/default `DATA_ROOT`. These seeds do **not** replace the writable,
durable content-addressed registry used for arbitrary supported A-share stocks
and ETFs.

```bash
.venv/bin/python scripts/prepare_cloudbase_bundle.py \
  --output /tmp/ashare-cloudbase-candidate \
  --snapshot-digest <300059-composite-digest> \
  --snapshot-digest <510300-composite-digest> \
  --code-revision <clean-40-character-sha>
```

The script refuses a dirty worktree, an identity mismatch, a non-Composite-v2
snapshot, missing files, altered hashes, duplicate digests, or an output path
inside the repository. It never copies `.git`, `.env`, credentials, caches,
tests, Choice `EmQuantAPI`, or unrelated snapshots. The Docker builder installs
the locked `demo` extra and runs an import smoke for `baostock` and
`pypdfium2`. Choice remains unavailable when the operator has not separately
provided its SDK/session; only the existing explicit public-data fallback may
then run.

## Temporary ephemeral candidate

Configure the new candidate with every value in
`ephemeral.env.example`, especially:

```text
DEPLOYMENT_PROFILE=ephemeral_candidate
APP_ENV=staging
DATABASE_URL=sqlite+pysqlite:////app/var/ephemeral/ashare.db
INITIALIZE_SCHEMA=true
PERSISTENCE_MODE=ephemeral
RESTART_RECOVERY_VERIFIED=false
SNAPSHOT_STORAGE_MODE=ephemeral_local
```

This profile is suitable only for current public HTTP smoke testing. Container
replacement may lose drafts, receipts, runs, and newly acquired snapshots.
Cross-process and cross-restart recovery are not acceptance claims.

The image-bundled Composite v2 seed is the first lookup target and is reused
when it already covers the submitted instrument and period. Only requests not
covered by a trusted seed enter on-demand acquisition; this avoids needless
provider calls without allowing stale or partial data to satisfy another
instrument or period.

## PostgreSQL migration gate (deferred TODO)

The CloudBase `ExecutePGSql` control-plane API accepts one statement per call;
therefore it cannot prove that `BEGIN + migration DDL + ROLLBACK` ran on one
connection. The previous multi-statement rehearsal and the dependent CLI push
path are disabled fail-closed. Before enabling strict production, run Alembic
from a VPC-connected operator/job using one real PostgreSQL connection and
prove upgrade, rollback, and re-upgrade against a disposable database or a
verified backup. Do not treat Preview/Push task success as rollback evidence.

## Provision the runtime role and enforce immutability

Role creation needs a secret password. Do it from a controlled VPC-connected
operator shell; never pass that password through `tcb api --body`, process
arguments, source control, logs, or the application container.

```bash
export DATABASE_ADMIN_URL='<operator-only PostgreSQL admin URL>'
export RUNTIME_DATABASE_PASSWORD='<secret with at least 24 characters>'
.venv/bin/python deploy/cloudbase/production_db.py provision-role \
  --runtime-role ashare_runtime

.venv/bin/python deploy/cloudbase/production_db.py apply-acl \
  --runtime-role ashare_runtime

export DATABASE_URL='<private PostgreSQL URL authenticating as ashare_runtime>'
.venv/bin/python deploy/cloudbase/production_db.py verify \
  --runtime-role ashare_runtime
```

The verifier checks the migration head, role attributes, object ownership,
table/schema ACLs and immutable triggers. It then inserts disposable linked
draft/plan/receipt/manifest/result/run rows as the actual runtime account,
proves `UPDATE` and `DELETE` are rejected, and rolls the whole probe back.

The management-plane verifier can independently confirm ACLs and rejection:

```bash
.venv/bin/python deploy/cloudbase/tc3_database_release.py apply-runtime-acl \
  --env-id <env-id> \
  --runtime-role ashare_runtime \
  --confirm-runtime-role ashare_runtime \
  --preflight-evidence /secure/evidence/db-preflight.json \
  --code-revision <clean-sha>

.venv/bin/python deploy/cloudbase/tc3_database_release.py verify-database \
  --env-id <env-id> \
  --runtime-role ashare_runtime \
  --alembic-head <release-head>
```

Old receipts and runs remain append-only. Receipt expiry requires a full new
validation and a newly inserted receipt; there is no receipt renewal.

## Durable snapshot registry

Provision the external mount before creating the candidate. Create distinct
`choice`, `technical`, `events`, `composite`, and `preparations` directories.
The supported CloudBase path is a read-write Tencent CFS mount in `ap-shanghai`,
on the same VPC/subnet as the CloudBase environment and PostgreSQL, mounted at
`/mnt/ashare-snapshots`. Set `SNAPSHOT_STORAGE_ID` to the recorded CFS file
system plus mount-point identity; an image directory, container `/tmp`, or an
unidentified object-store/FUSE mount is not accepted production evidence.
Write `.ashare-snapshot-store.json` on that mount with this non-secret shape:

```json
{
  "schemaVersion": "ashare-lab.durable-snapshot-store.v1",
  "storageId": "<provider storage resource id>",
  "restartProbeId": "<random non-secret probe id>",
  "durable": true
}
```

Restart a zero-traffic candidate instance and require the same marker and probe
to remain readable before recording the probe ID in the candidate environment.
The entrypoint rejects missing/mismatched markers, roots under `/app` or `/tmp`,
non-distinct roots, and roots the runtime cannot read and write. Newly acquired
snapshots remain immutable content-addressed children of these roots; temporary
preparation files never become runnable snapshots.

For this release keep one service instance and one local worker thread. The
existing on-demand adapter already serializes one process and publishes a
Composite before selecting its fixed content ID; cross-instance acquisition
locking is not yet proven. A submitted strategy may query any supported A-share
STOCK/ETF, acquire BaoStock reference data plus Choice or Eastmoney Push2 daily
data and Eastmoney events, publish immutable source snapshots and the final
Composite on CFS, then execute only from that selected Composite ID. Unsupported
coverage still fails closed; it is never replaced with a bundled seed or a
different instrument.

## Strict-production candidate, smoke test, and rollback (future)

1. Save the stable version name, stable SHA, database preflight/rehearsal hashes
   and current traffic allocation as immutable release evidence. If the
   platform later exposes a verified physical PostgreSQL backup API, record its
   ID separately; this release tooling does not invent one.
2. Create a **new** CloudBase Run version from the bundle, attach the database
   VPC/subnet, inject only the runtime `DATABASE_URL`, and keep default traffic
   at 0%.
3. Verify `/api/v1/ready`, then run the stock and ETF strategy-v2 HTTP flows,
   index/financial/event rejection, restart recovery, manifest traceability,
   and database tamper probes against candidate-only routing.
4. Route traffic only after all gates pass. Any core failure keeps candidate
   traffic at 0%.
5. Roll back by restoring the recorded stable version to 100% and candidate to
   0%. Database artifacts are append-only; do not rewrite or delete them. Use
   the selected backup only if a migration rollback is explicitly required and
   verified separately.

Container contract:

- port `8000`, health path `/api/v1/ready`;
- explicit `DEPLOYMENT_PROFILE=strict_production`;
- `APP_ENV=production`, `INITIALIZE_SCHEMA=false`;
- exact `CODE_REVISION` and strict snapshot identities;
- exact active WorkBuddy HTTPS origins in `CORS_ALLOWED_ORIGINS`;
- one API worker for the current in-process thread queue.

The WorkBuddy frontend may use the candidate only with `VITE_USE_MOCK=false`
and its candidate HTTPS API URL. Local readiness, fixture tests, or a static
frontend build are not public end-to-end evidence.
