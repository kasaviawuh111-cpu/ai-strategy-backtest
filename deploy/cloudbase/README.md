# CloudBase Run backend Demo

This target packages one immutable, locally validated Composite v2 snapshot and
runs FastAPI plus the in-process backtest worker in a single container. It is a
research Demo deployment, not the production multi-container topology.

## Build contract

- Dockerfile: `deploy/cloudbase/Dockerfile`
- container port: `8000`
- health path: `/api/v1/ready`
- required build argument: `CODE_REVISION=<exact clean 40-character Git SHA>`
- runtime environment: copy `deploy/cloudbase/env.example` and replace the SHA

Create a standalone upload directory first. The script accepts only a clean Git
SHA, validates the content-addressed snapshot and every registered file, and
copies an allowlist of source files. It never copies `.git`, `.env`, credentials,
caches, test artifacts, or unrelated snapshots.

```bash
.venv/bin/python scripts/prepare_cloudbase_bundle.py \
  --output /tmp/ashare-cloudbase-bundle \
  --snapshot-digest <validated-composite-digest> \
  --code-revision <exact-clean-sha>
```

Upload the resulting empty-to-new bundle directory as the CloudBase source.
The build fails if its selected snapshot payload is absent. Application
startup additionally rejects a mismatched digest, invalid manifest, incomplete
company-action/event coverage, or a non-40-character code revision.

## Demo persistence and limits

SQLite and run artifacts live under `/tmp`, so CloudBase instance replacement
can remove historical run records. The immutable market/event snapshot is part
of the image and is always replayable; a later persistent PostgreSQL deployment
is required before treating run history as durable.

Use one API worker. The configured thread queue runs real backtests in the same
process; horizontal replicas do not share their SQLite run stores. Set minimum
instances to one for the review window, or move to the repository's
PostgreSQL+Redis+RQ topology before scaling out.

## Public verification

After CloudBase provides the HTTPS URL:

```bash
curl -fsS https://<service>/api/v1/ready
curl -fsS https://<service>/api/v1/capabilities
.venv/bin/python scripts/live_strict_e2e.py \
  --base-url https://<service> \
  --strategy technical \
  --expected-code-revision <sha> \
  --expected-producer-snapshot-id composite:<digest>
.venv/bin/python scripts/live_strict_e2e.py \
  --base-url https://<service> \
  --strategy event \
  --expected-code-revision <sha> \
  --expected-producer-snapshot-id composite:<digest>
```

The WorkBuddy build must use `VITE_USE_MOCK=false` and the CloudBase HTTPS URL
as `VITE_API_BASE_URL`. The browser origin must exactly match
`CORS_ALLOWED_ORIGINS`.
