# CloudBase Run backend Demo

This target packages one immutable, locally validated Composite v2 snapshot and
runs FastAPI plus the in-process backtest worker in a single container. It is a
research Demo deployment, not the production multi-container topology.

## Build contract

- Dockerfile: `deploy/cloudbase/Dockerfile`
- container port: `8000`
- health path: `/api/v1/ready`
- generated bundle pins `CODE_REVISION=<exact clean 40-character Git SHA>` in
  its root Dockerfile; CloudBase does not need a separate build argument
- default browser origin: the exact WorkBuddy HTTPS origin; a platform
  `CORS_ALLOWED_ORIGINS` environment variable may override it

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
The bundle preserves the content digest as the snapshot directory name, and the
rendered Dockerfile pins both that digest and the clean code revision. The build
fails if its selected snapshot payload is absent. Application startup
additionally rejects a mismatched digest, invalid manifest, incomplete
company-action/event coverage, or a non-40-character code revision.

CloudBase CLI 3.8.1 requires the container port on source deployments. Run the
command from any directory; `--source` must point to the generated bundle, not
the repository or the tar file. `--install-dependency false` prevents the
platform from trying a second language-level dependency installation outside
the Docker build.

```bash
tcb --env-id <environment-id> cloudrun deploy \
  --service-name <service-name> \
  --source /tmp/ashare-cloudbase-bundle \
  --port 8000 \
  --install-dependency false \
  --wait
```

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
  --output /tmp/ashare-public-live.json
```

The WorkBuddy build must use `VITE_USE_MOCK=false` and the CloudBase HTTPS URL
as `VITE_API_BASE_URL`. The browser origin must exactly match
`CORS_ALLOWED_ORIGINS`.
