# Distributed collection: one coordinator, D1, D2 and C1-C7

This is a manually operated batch file protocol. PostgreSQL contains metadata;
immutable run directories contain the data. There is no worker API, scheduler,
Google Sheets sync, automatic worker activation, or production migration runner.
The tools leave T-020/T-020A and canonical contract 0.1.0 unchanged.

## Setup and permissions

On a clean checkout, after this feature is merged:

```powershell
git pull --ff-only origin main
uv sync --extra dev --frozen
rclone version
rclone config
rclone lsd reposeer:
$env:REPOSEER_DRIVE_REMOTE = 'reposeer:'
$env:TEMP = Join-Path $env:LOCALAPPDATA 'Temp'
$env:TMP = $env:TEMP
```

Install rclone externally (for example `winget install Rclone.Rclone`). Each user
configures OAuth once; never distribute rclone.conf, OAuth tokens, `.env`, GitHub
tokens, service-role keys or the registry DSN in assignments/Drive/Sheet. Configure
restricted folder permissions: D1 discovery output; D2 its assignments and screening
output; Cn its own assignments/output; coordinator reads submissions and writes
canonical. An OAuth remote name does not itself enforce Drive ACLs. Verify those
ACLs manually before enabling any slot. The scripts check rclone version/list
access when a transfer is requested. They never create public views/RPC or change RLS.

Only the coordinator sets a secret DSN, using an unlogged prompt:

```powershell
$secret = Read-Host 'Coordinator PostgreSQL DSN' -AsSecureString
$env:REPOSEER_REGISTRY_DSN = [System.Net.NetworkCredential]::new('', $secret).Password
```

D1/D2/C1-C7 do not get this credential. D2 and worker commands reject an exported
`REPOSEER_REGISTRY_DSN`. GitHub tokens remain in each crawler's local environment
or local `.env` for the existing collector. The coordinator Registry reads its DSN
only from the environment, not automatically from `.env`.

On Supabase SQL Editor, compare the existing bootstrap with
`supabase/migrations/20261021000100_create_registry.sql` (copied verbatim from the
supplied bootstrap). **Do not reapply it to existing production.** Apply only
`20261021000200_coordination_assignments.sql`, after backup/review. It is additive
and repeatable; no worker is enabled. Tools check columns, private table RLS and
unique indexes before operating, and fail rather than automatically migrating.

```powershell
uv run python tools/coordination/dispatcher.py status --json
```

After dry-runs and folder permission checks, enable named slots individually:

```powershell
uv run python tools/coordination/dispatcher.py activate --worker D1 --display-name 'Discovery operator'
uv run python tools/coordination/dispatcher.py activate --worker D2 --display-name 'Screening operator'
uv run python tools/coordination/dispatcher.py activate --worker C1 --display-name 'Crawler 1'
```

Repeat explicitly for C2-C7 using real assigned names. Disabled slots cannot be
dispatched. `activate` is an explicit DB write; do not run during code verification.

## D1: discover names, never crawl

The coordinator streams sorted current registry IDs into an exclusions snapshot:

```powershell
uv run python tools/coordination/dispatcher.py export-exclusions --output data/coordination/exclusions.txt
rclone copyto data/coordination/exclusions.txt reposeer:discovery/exclusions.txt
```

D1 downloads the exclusions and a previously verified index snapshot plus JSON
sidecar, then runs the existing sampler. Replace the quoted snapshot path:

```powershell
rclone copyto reposeer:discovery/exclusions.txt data/discovery/exclusions.txt
uv run reposeer discover-packages --source snapshot --snapshot 'data/discovery/shared/<sha256>.html' --count 10000 --seed 20261021 --batch-size 50 --exclude-file data/discovery/exclusions.txt --data-dir data/discovery/D1
```

Use the actual UUID from the summary (`$sampling` below). Upload the two required
files; keep the full original sampling directory/snapshot archived for provenance:

```powershell
$sampling = '<sampling-UUID>'
$run = "data/discovery/D1/registry/runs/$sampling"
rclone copyto "$run/manifest.json" "reposeer:discovery/submissions/$sampling/manifest.json" --immutable
rclone copyto "$run/package_candidates.csv" "reposeer:discovery/submissions/$sampling/package_candidates.csv" --immutable
```

Coordinator copies that UUID directory and validates/imports it. Only CSV is
mandatory alongside manifest: D1 deliberately need not transfer targets/batches.
Their hashes are checked too if those optional files are supplied. CSV SHA, schema,
run UUID, rank, canonical identity and initial states are checked before any DB write.

```powershell
rclone copy "reposeer:discovery/submissions/$sampling" "data/coordination/inbox/discovery/$sampling"
uv run python tools/coordination/dispatcher.py import-discovery --input-dir "data/coordination/inbox/discovery/$sampling" --dry-run
uv run python tools/coordination/dispatcher.py import-discovery --input-dir "data/coordination/inbox/discovery/$sampling"
```

Repeated imports preserve original package mapping/provenance and create no extra
package/screen job. Example actual counter names:
`{"names_seen":1000,"packages_inserted":950,"duplicates_skipped":50,"screen_jobs_created":950}`.
For dashboard display, packages_seen = names_seen, new_packages = packages_inserted.

## D2: screen current source metadata

```powershell
uv run python tools/coordination/dispatcher.py dispatch-screen --worker D2 --limit 100 --out-dir data/coordination/assignments/D2 --lease-hours 48
```

This snapshots and claims one assignment, commits, then publishes/uploads its JSON.
If upload fails, repeat the exact command/out-dir: pending journals are published
with the original token before any new job is claimed. Keep `.pending` journals.
After a DB rollback leaves a journal, inspect its claim mismatch; do not invent a
new token, delete history, or force a job complete.

D2 downloads the actual assignment ID from the coordinator, then runs:

```powershell
$assignment = '<assignment-UUID>'
rclone copyto "reposeer:screening/assignments/D2/$assignment.json" "data/coordination/assignments/D2/$assignment.json"
uv run python tools/coordination/dispatcher.py screen-local --assignment "data/coordination/assignments/D2/$assignment.json" --out-dir data/coordination/screening/D2 --remote reposeer:
```

Requests are sequential through the existing bounded PyPI HTTP client. Only the
T-020 Source/Repository/Code rule yields a candidate; Home, missing and conflicting
URLs remain unresolved. A candidate is **not verified_current**. Saved PyPI JSON is
canonical serialization of the parsed API response; its SHA describes those saved
bytes, not HTTP wire bytes. Results retain retrieval time and checksum. A network
failure is `failed`, leading to bounded coordinator retry rather than unresolved.
D2 retains local files and uploads READY last. A repeated upload reuses saved results.

```powershell
rclone copy "reposeer:screening/submissions/D2/$assignment" "data/coordination/inbox/screening/$assignment"
uv run python tools/coordination/dispatcher.py import-screening --input-dir "data/coordination/inbox/screening/$assignment" --dry-run
uv run python tools/coordination/dispatcher.py import-screening --input-dir "data/coordination/inbox/screening/$assignment"
```

Import re-runs the source rule from saved evidence and checks each current claim.
An already verified package keeps its repository/mapping unchanged; candidate-only
screening cannot relink it. Case-equivalent repository candidates share one repository and one initial crawl
job, even when many packages refer to it. Re-import does not increase attempts.

## C1-C7: one sequential collector per machine

Coordinator assigns at most 10 repository jobs per call, without reusing live claims:

```powershell
uv run python tools/coordination/dispatcher.py dispatch-crawl --worker C1 --limit 10 --out-dir data/coordination/assignments/C1 --lease-hours 48
```

A crawl assignment path is a pointer to the current DB claim: reissuing the same job
updates `crawl/assignments/Cn/<job_id>.json` only while holding that claim's row lock.
Prior local snapshots are retained under the dispatch out-dir's `history/<sha>.json`;
previous run bundles and their assignment copies remain immutable. Disabled workers
cannot receive either new assignments or recovered pending uploads.

Assignments contain up to 100 linked PyPI targets, fixed bounded collector options,
expected repository, token and checksum. If a repository has over 100 packages,
`package_scope_truncated=true`; unassigned/unverified packages stay candidates.

Crawler downloads one actual job JSON and validates without API/upload first:

```powershell
$job = '<job-UUID>'
rclone copyto "reposeer:crawl/assignments/C1/$job.json" "data/coordination/assignments/C1/$job.json"
uv run python tools/coordination/worker.py run --worker C1 --assignment "data/coordination/assignments/C1/$job.json" --data-root data/coordination/work/C1 --remote reposeer: --dry-run
uv run python tools/coordination/worker.py run --worker C1 --assignment "data/coordination/assignments/C1/$job.json" --data-root data/coordination/work/C1 --remote reposeer:
```

The job's data-dir is exactly `<data-root>/<job_id>`. The subprocess uses an argument
array and existing `collect-dataset` flags. No arbitrary command/URL is executed.
A child exit 1 with a genuine partial manifest and raw observations is submitted;
its data is retained. A missing manifest creates a local failure receipt and fails,
never a fabricated integration run. Report that failure to the coordinator; it has
no fake successful READY run. Logs are redacted and retained outside the bundle.

Retry the same command to upload saved READY data without collecting again. Local
files/checkpoints are never deleted automatically. An unfinished T-020 manifest
requires operator inspection and existing child recovery commands: T-020 has no
integration resume engine. A reissued claim must not overwrite an old claim's
job directory; retain/archive the old root and select a fresh local `--data-root`.
Use the default 48-hour lease, longer than expected collection and upload time.

## Coordinator: ingest, audit and catalog

Use local C: TEMP for Parquet work. Workspace files are verified downloads; they
may be large. No mutable DuckDB is opened on Drive Stream.

```powershell
uv run python tools/coordination/ingest.py run --remote reposeer: --workspace data/coordination/central --dry-run
uv run python tools/coordination/ingest.py run --remote reposeer: --workspace data/coordination/central --limit 25
uv run python tools/coordination/dispatcher.py status --json
uv run python tools/coordination/dispatcher.py requeue-expired --pending-dir data/coordination --dry-run
# Only after reviewing current READY, pending assignments/uploads and worker state:
uv run python tools/coordination/dispatcher.py requeue-expired --pending-dir data/coordination --apply
```

`--dry-run` makes no DB or Drive writes; worker dry-run also creates no work files.
Only READY submissions are downloaded. SHA/size lists cover every submitted file
except CHECKSUMS and READY themselves. Claims, targets, job, worker, assignment hash,
run and canonical raw provider evidence are revalidated. Stale tokens, renamed-ID
collisions and malformed/tampered bundles are quarantined locally, not silently
merged. Arbitrary archives/symlinks, traversal and oversized bundles are rejected.
Limits: 2 GiB/file, 20 GiB/bundle, 10,000 files, 8 MiB structured JSON, 16 MiB raw line.

Canonical accepted bundles are immutable `canonical/runs/<integration_run_id>/`.
They preserve all original raw bytes, observations, nulls, processed schemas and
worker-local absolute provenance paths. Resolve run artifacts using their relative
paths under the catalog run root; don't assume a worker's disk path exists centrally.
A bundle hash is the SHA of CHECKSUMS.sha256; bundle byte_size counts all files.
Previous accepted submissions are skipped by their READY payload checksum and DB
artifact path; downstream authority is the coordinator-owned canonical copy.

Artifact acceptance differs from job collection. A partial run ends the bounded
initial repository job only when at least one assigned package is independently
verified against current PyPI/GitHub raw evidence and repository metadata was
actually stored. Activity budget_exhausted remains partial in run/coverage; there
is no promise of complete history. Only individually verified package IDs are
promoted. Unverified mapping/metadata runs are retained for audit and the job retries
with bounded backoff, then fails at max_attempts. No abandonment label is produced.

Canonical upload/read-back verification precedes DB ingestion. Failure rolls back
acceptance; replay reuses matching files and never overwrites a conflicting SHA.
A catalog failure may leave accepted DB runs and a stale active catalog; repeat ingest
even with no pending bundles to rebuild it from DB plus accepted receipt checksums.
The explicit-schema catalog is built atomically on local TEMP, uploaded as immutable
`canonical/catalog/snapshots/<sha>.parquet`, verified, then publishes
`canonical/catalog/ingestion_index.parquet` and `active.json`. Consumers read active.json,
resolve its immutable relative_path and verify SHA; do not read a half-published alias.
No Parquet concatenation or feature construction is performed. Operate one coordinator
writer; do not overlap scheduled ingest processes.

Requeue is dry-run by default, fails closed if Drive cannot be queried, checks current
claim tokens against remote/local pending files, and clears claim fields only when
applying a reclaim. Incomplete uploads with unknown ownership conservatively block
reclaim. Attempts/history are never reset. Resolve quarantines/pending journals by
inspection; do not blindly delete evidence or manually mark jobs collected.

Status JSON includes package mapping, repository verification, job kind/status, the
nine worker slots/assignment counts and artifact statuses. Suggested dashboard
metrics are packages_seen/new_packages (per import), screened, candidate_repositories,
jobs_queued, jobs_claimed, artifacts_ingested, jobs_completed, jobs_retry and
per-command duplicates_skipped. Do not invent historical aggregate duplicate counts:
they are not stored by the current schema. Example ingestion result:
`{"artifacts_ingested":4,"jobs_completed":1,"jobs_retry":0,"accepted":[{"overall_status":"partial","artifact_accepted":true,"job_collected":true}]}`.
The existing Sheet is a manually maintained dashboard; `status --json` is the input
for a separate future sync, never a distributed lock. No background task is installed.
After manual success, Task Scheduler can invoke the ingest command periodically
with **Do not start a new instance** configured; do not store secrets in task arguments.

## Offline verification and production checklist

```powershell
uv sync --extra dev --frozen
uv run ruff check tools/coordination tests/coordination
uv run pytest tests/coordination -q
uv run reposeer discover-packages --help
uv run reposeer collect-dataset --help
```

The tests fake rclone/subprocess metadata storage, and reuse real offline T-020 raw
observations. PostgreSQL concurrency is opt-in via REPOSEER_COORDINATION_TEST_DSN;
only localhost and a database named `reposeer_coordination_test...` are accepted.
Use a dedicated disposable local PostgreSQL database with schema/role creation
privileges. Never point this test at Supabase. It applies the two migrations only
there, removes only its own UUID rows and checks competing SKIP LOCKED transactions.
Without that DSN this test is skipped; offline tests cannot establish live SQL/ACL
compatibility or provider quotas.

Before production: review/apply additive migration; verify private schema/RLS/indexes;
secure coordinator DSN; configure restricted Drive OAuth separately; test dry-run;
activate named slots explicitly; run one small discovery/screen/crawl/ingest batch;
verify SHA read-back and catalog pointer; inspect partial/failed counts; then scale.
No production DB/Drive state, worker activation or real assignments are changed by
implementing/testing this task. A current PyPI snapshot is not historical evidence;
a pilot's 30% verified out of 20 samples is not a population verification rate.

References: [rclone copy](https://rclone.org/commands/rclone_copy/),
[rclone exit codes](https://rclone.org/docs/#exit-code),
[psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html).
