# Distributed collection with mounted Google Drive

The recommended student workflow uses Google Drive for desktop as a mounted folder.
**It does not use Supabase, SQL, rclone, claims, leases, or Central Ingestion.**
D1 sampling remains deterministic; Drive exclusions prevent resampling screened packages.
PyPI/GitHub contracts remain unchanged; external V2 adds five configured providers.
The older Registry workflow is retained separately under **Legacy** below; do not
mix its JSON assignments or ingestion commands with the new CSV workflow.

## C1-C7 external V2

Follow [the Vietnamese C1-C7 guide](C1-C7_EXTERNAL_COLLECTION.md) for personal API
setup, `check-providers`, and external-only `enrich-drive --submissions-dir`.
New D2 READY files publish `collection_policy.policy_id=distributed-v2`; workers
resolve immutable limits from that verified policy without changing `crawl-drive`.
READY files without policy retain `legacy-initial`. Published submissions stay
immutable; provider supplements write beside their source part under
`submissions/<worker>/<run>/<repo>/supplements/<part>/external-v2/packages/<package_sha256>/<provider>/`.
New D2 staging records policy intent before screening; an older unfinished stage
without intent requires inspection instead of silently changing its policy.
V2 requires local GitHub/DEV.to/Stack Exchange/NewsAPI credentials and configured
public HTTPS RSS feeds. Enrichment uses only the external preflight and never
calls GitHub/PyPI, including for authentication. Static configuration is mandatory;
external quota/availability failures defer only the affected provider. Independent
provider READY/checksums prove completion; mutable online state lives under
`supplements/<part>/external-v2/state/<package_sha256>/<provider>.json`.
Both worker commands share one planner. `crawl-drive` automatically supplements at
most one incomplete package of its current assignment; `enrich-drive` handles all
historical parts and ignores the sibling `supplements` directory. Terminal policy
budgets are not retried, and the 20-URL package budget includes initial captures.
`dispatcher.py external-report --drive-root ...` writes a separate derived
`screening/external_coverage.csv`; C1-C7 never write master mapping or this view.
`tools.coordination.external.resolve_verified_external_inputs` provides verified
initial/supplement observations and coverage. Offline `reposeer process` requires
`--output-dir` when reading a sealed source. Dry-run is offline and read-only.

## One-time student setup (Windows CMD)

Install Python 3.12+, Git and Google Drive for desktop, then obtain access to the
shared `RepoSeer-Data` folder. Every member's mounted absolute path may differ.
Once the updated branch is available:

```cmd
py -m pip install uv
git clone --branch feat/distributed-crawl-coordination https://github.com/ldatdzs1tg/RepoSeer.git "%USERPROFILE%\RepoSeer"
cd /d "%USERPROFILE%\RepoSeer"
uv sync --frozen
notepad .env
```

Crawlers configure their personal credentials and RSS feeds in the private local
`.env` as described in the V2 guide; never copy it to shared storage. No database credential or rclone configuration is needed.
Crawlers need read access to the D1 inputs, screening run and all seven assignment
CSVs for cross-worker checks, plus write access to their own submissions folder.
Allow Drive Desktop to download input files before running the commands below.

## One-time mapping migration or fresh-folder initialization

Before using D1 or D2, run these commands against your mounted shared folder:

```cmd
uv run --frozen python tools/coordination/dispatcher.py bootstrap-mapping --drive-root "<RepoSeer-Data>" --dry-run
uv run --frozen python tools/coordination/dispatcher.py bootstrap-mapping --drive-root "<RepoSeer-Data>"
```

Bootstrap alone scans completed historical runs. It verifies every run's READY,
evidence, D1 input and assignments, orders runs by READY generation time then run ID,
and retains the first screening row for each package. Historical files remain
immutable. An existing legacy latest-run alias must exactly match the latest verified
run before replacement; unexpected bytes require inspection. A fresh folder with no
completed runs or legacy alias gets a header-only master. Once initialized, bootstrap
only validates the master and reports `already_initialized`; it never silently rebuilds
a missing or corrupt master. Dry-run performs no writes.

## D1: sample new packages using the cumulative master

```cmd
uv run --frozen reposeer discover-packages --source pypi-simple --count 1000 --seed 20261024 --batch-size 50 --drive-root "<RepoSeer-Data>" --data-dir data
```

Replace `<RepoSeer-Data>` with the absolute shared folder path, not `mapping.csv`.
D1 verifies `screening/mapping.csv` against `mapping.meta.json` and excludes all
previously screened packages, including unresolved, ambiguous and failed results.
An optional `--exclude-file` is normalized and combined with master exclusions in a
local temporary file; your original exclusions and shared files are never changed.
The existing sampling algorithm, seed, ordering, CSV and manifest schemas are retained.
The manifest's exclusion hash/count describe the actual combined exclusion file.
D1 rechecks master metadata before committing its sample and before reporting success;
if it changes, discard that sample and start a new one. Missing metadata, bad hashes,
malformed/duplicate/unsorted rows or an unfinished update fail closed. Without
`--drive-root`, the original discovery behavior and `--exclude-file` remain supported.

Using the Sampling Run ID printed by this command, copy **unchanged**
`data\registry\runs\<run_id>\manifest.json` and `package_candidates.csv` to shared
`discovery\submissions\<run_id>\`. Keep the remaining D1 artifacts locally.
D1's sampling, ordering, CSV schema and package identities are not modified.

## D2: screen and allocate in one command

Copy the absolute path of the D1 run folder from Explorer:

```cmd
uv run --frozen python tools/coordination/dispatcher.py screen-drive --run-dir "<RepoSeer-Data>\discovery\submissions\<run_id>"
```

Replace the example path with your own mounted path. D2 validates the original D1
CSV hash/schema/order and screens new packages through the existing PyPI client
and Source/Repository/Code mapping rule. Results remain `candidate`, `unresolved`,
`ambiguous` or `failed`; discovering a URL does not establish a verified mapping.
D2 automatically skips packages already in the master, including failed, unresolved
and ambiguous results, while preserving D1 order and original inputs. Only new
packages enter the per-run mapping/evidence and candidate assignments; progress counts
only these packages. Dry-run reports `package_count` (input), `to_screen`,
`skipped_existing_count` and up to five `skipped_existing_examples`. Completed results
keep `package_count` as screened rows and add `input_package_count` and the skip fields.
READY records `skipped_existing_package_ids`; old READY files default to no skips.
A fully duplicate run publishes empty mapping/evidence and seven header-only assignments,
without PyPI requests or a master rewrite. Published runs are verified and reused,
so retries preserve their recorded skip list without re-screening.

D2 automatically shows a live progress bar on stderr with processed/total, percentage,
elapsed time and estimated time remaining. Mapped counts repository candidates;
Unresolved means no candidate was found, Ambiguous means conflicting candidates,
and Failed means screening could not complete (including HTTP 404). Unmapped is the
sum of Unresolved, Ambiguous and Failed. Cached counts validated checkpoints reused
without another PyPI request. Counts advance only after a persisted result is loaded
or saved. Add `--no-progress` to suppress progress. When stderr is redirected to a
log, D2 writes a plain snapshot every 25 packages and at completion, without terminal
control sequences. The final result JSON stays on stdout. Dry-runs and already
published runs do not start progress.

Repositories are grouped case-insensitively, sorted by descending package count
then repository name, and assigned to the worker with the fewest packages, then
fewest repositories, then lowest worker ID. Packages within a group are sorted.
This is deterministic, never splits a repository between workers, and does not
exclude repositories merely because an earlier sampling run contained them.
Empty workers receive a header-only CSV. Compact PyPI evidence stores the original
name/project_urls used for each successful decision and its canonical JSON SHA-256.

D2 stages outputs at `C:\RepoSeerData\screening\<run_id>\`, checkpoints each package,
copies via temporary files, verifies every hash/size by read-back, then publishes
`screening/runs/<run_id>/READY.json` last. READY includes hashes for mapping, evidence,
all seven assignments, and the original D1 inputs. Retry the same command after a
copy interruption: it reuses local screening results. A published run is verified
and reused without PyPI requests. Conflicting published bytes are never replaced.
After per-run READY publication, D2 merges into cumulative `screening/mapping.csv`.
The existing nine-column mapping schema is retained, sorted by canonical package ID,
with one immutable first-seen row per package. All four statuses are retained; this
master is not evidence of successful repository collection. Identical replays make no
writes. Historical overlaps from different runs retain the original master row and
are reported; conflicting content from the same run fails. Final JSON retains the
existing fields and adds `master` counters: `previously_stored`, `new_packages_added`,
`historical_duplicates`, and `total_unique_packages`.

Only D2 (including bootstrap) writes the master. It stages CSV processing locally,
verifies bytes, writes `mapping.pending.json`, checks the previous master again,
replaces the CSV through a verified temporary copy, then publishes metadata and
verifies both before removing the journal. Metadata contains schema version, exact
CSV SHA-256, row count, UTC update time, and last merged run ID (null for an empty
master). D1 refuses to sample while a journal exists. Rerun D2 or bootstrap after an
interruption: it verifies the journal's immutable source runs and either retries the
old validated state, completes metadata for the verified new CSV, or removes a verified
stale journal. Unverifiable states retain all files and require inspection; do not
delete the journal or replace conflicting files to force recovery. Every reader
verifies its own mounted bytes because Drive synchronization may lag.

Run D2 commands sequentially and wait for Drive synchronization before other members
sample. Exclusive journal creation detects competing updates on the same mount;
there is no cross-machine distributed lock. Normal D1 and D2 operations read the
single master without scanning history. Its streaming reader has a separate 512 MiB
file safeguard and 1 MiB line safeguard, without the per-run 10,000-row limit;
per-run and assignment limits remain unchanged.

## C1-C7: one CSV path, one command

Each crawler opens their own assignment folder and copies its CSV's absolute path:

```cmd
uv run --frozen python tools/coordination/worker.py crawl-drive --assignment "G:\...\RepoSeer-Data\assignments\C1\<run_id>.csv"
```

C2-C7 change only the path; worker and sampling run are derived from it. The command
verifies D2 READY, mapping/evidence, and all seven assignment hashes and allocation
before collection. A not-yet-synced, missing or changed input fails safely.
It calls the existing `uv run --frozen reposeer collect-dataset` with initial limits:
1 page / 30 records per GitHub group; hackernews / 1 page / 1 record / 1 candidate.
A repository with more than 100 packages is split into sorted `part_001`, `part_002`,
etc. of at most 100 packages, all under the same crawler. Every assigned package
must be verified by the collected PyPI/GitHub evidence against its assigned repository;
a changed or unresolved mapping preserves local evidence and reports failure.

Each part is uploaded immediately with raw, processed, metadata, reports, targets
and collection-input provenance. CHECKSUMS covers every payload; READY is written
last and records the actual integration run and `complete` or `partial` status.
Logs stay local, outside the submitted part, and are redacted. Summary counts use
repository parts (`unit: repository_parts`), including completed, partial, failed
and skipped_existing. A reported failure makes the CLI exit nonzero.

## Output and safe retries

```text
RepoSeer-Data/
  discovery/submissions/<run_id>/manifest.json, package_candidates.csv
  screening/mapping.csv                         # cumulative, all screened statuses
  screening/mapping.meta.json                   # master checksum and row count
  screening/mapping.pending.json                # only during an unfinished update
  screening/runs/<run_id>/mapping.csv, pypi_evidence.jsonl, READY.json
  assignments/C1/<run_id>.csv                   # C1 through C7
  submissions/C1/<run_id>/<repository_id>/part_001/
    COLLECTION_INPUT.json, targets.txt
    raw/, processed/, metadata/, reports/
    CHECKSUMS.sha256, READY.json
```

`repository_id` is the full 64-character SHA-256 of the lowercase `owner/repo`.
Crawlers work locally at `C:\RepoSeerData\work\Cn\<run_id>\<repository_id>\part_001\`.
Use `--data-root` to override the local base on another machine; Linux/macOS default
to `~/.local/share/reposeer/screening` and `~/.local/share/reposeer/work`. Never select
a Drive-mounted directory for computation, checkpoints or Parquet processing.

Rerun the same command and retain the same local workspace. A fully verified Drive
part is skipped even on a fresh machine. A finished local run is uploaded again
without new collection if its upload was interrupted. Modified/missing declared
files or unexpected ordinary files fail; only undeclared `desktop.ini` is ignored.
Symlinks/junctions, traversal and credential paths are rejected. Interrupted runs
without a finished integration manifest require inspection of the existing child
checkpoints; the integration pipeline has no automatic resume engine. Do not delete
raw data or overwrite partial/conflicting shared files to make a retry succeed.
A remote partial upload with no corresponding local workspace is left for inspection.

`--dry-run` validates inputs and reports the plan without API calls or writes.
Run one D2 command at a time and one crawler process per assignment; no distributed lock is
provided. READY means the publisher verified its mounted copy, **not** that every
other machine has synced it. Each reader independently verifies all required files.
Cross-run deduplication applies to package screening, not repository collection. New submissions are
self-contained inputs for a future offline aggregation step; **legacy ingest.py
does not consume them**. No ML labels or dataset contracts are changed.

Offline validation:

```cmd
uv run --frozen ruff check src/reposeer/cli tools/coordination tests/coordination
uv run --frozen python -m pytest tests/coordination/test_drive_only.py -q
uv run --frozen python -m pytest tests/integration/test_package_discovery.py -q
```

## Legacy: Supabase/rclone workflow

The instructions below apply only to the earlier Registry-based JSON assignments.
They are not prerequisites for `screen-drive` or `crawl-drive`. Existing assignments,
submissions and Registry records are neither migrated nor modified automatically.

This is a manually operated batch file protocol. PostgreSQL contains metadata;
immutable run directories contain the data. There is no worker API, scheduler,
Google Sheets sync, automatic worker activation, or production migration runner.
The tools leave T-020/T-020A and canonical contract 0.1.0 unchanged.

### Team member quick start

#### One-time setup (Windows CMD)

Once the coordinator shares the updated branch, install Python 3.12+ and Git first.
Use a local checkout and your own GitHub token:

```cmd
py -m pip install uv
winget install --id Rclone.Rclone --exact
git clone --branch feat/distributed-crawl-coordination https://github.com/ldatdzs1tg/RepoSeer.git "%USERPROFILE%\RepoSeer"
cd /d "%USERPROFILE%\RepoSeer"
uv sync --frozen
rclone config
rclone lsd reposeer:
notepad .env
```

In `.env`, add `GITHUB_TOKEN=your-own-token`. Keep this file private and outside the
shared data folder. Do not copy the coordinator's environment file. If you already
have the checkout/tools, skip their installation and cloning steps.

In `rclone config`, name the remote `reposeer` and point it at the shared
`RepoSeer-Data` folder. Use an alias to your mounted shared folder for Drive for
desktop, or a Google Drive OAuth remote. Ask the coordinator for access to your
own assignments, submissions, inbox and processed inbox. Transport compatibility
settings and checksums are handled by the command; no per-transfer filters are needed.

#### Daily command

From your checkout, run one assigned job and exit:

```cmd
uv run --frozen python tools/coordination/worker.py work --worker C1
```

Use your assigned ID (`C2` through `C7`) in place of `C1`. `status: submitted`
means the immutable bundle and inbox notification were published; `overall_status`
may be partial. `status: no_work` means no eligible assignment remains.
Results go to shared `crawl/submissions/C1/` and `crawl/inbox/C1/` automatically.
Keep `C:\RepoSeerData\coordination\work\C1`: rerun the same command after a transfer
failure to reuse collected data. For ambiguity, expired work, or an unfinished crawl,
send the summary/error code to the coordinator and keep your local files.

### Coordinator operations

Members only need the quick start above. The coordinator retains Registry access,
runs the existing D1/D2 steps below, and dispatches at most **one unfinished eligible
assignment per worker** for normal automatic selection. Different workers run in parallel.
Use the existing commands, with C2-C7 and their own output directories as needed:

```cmd
uv run --frozen python tools/coordination/dispatcher.py dispatch-crawl --worker C1 --limit 1 --out-dir "C:\RepoSeerData\coordination\assignments\C1" --lease-hours 48
uv run --frozen python tools/coordination/ingest.py run --workspace "C:\RepoSeerData\coordination\central"
uv run --frozen python tools/coordination/dispatcher.py status --json
```

Drive folders provide evidence without another progress database: `assignments/`
contains published claims; `submissions/.../READY.json` identifies a sealed run;
`inbox/` contains pending notifications; `inbox-processed/` contains archived accepted
notifications; canonical artifacts live under `canonical/runs/`. Acceptance can
retain a partial run while its job retries. These folders do not show a live crawl's
progress and cannot establish current Registry status; Supabase remains authoritative.
An optional overview Sheet is maintained manually.

#### Setup and permissions

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
supplied bootstrap). **Do not reapply it to existing production.** After backup/review, apply
`20261021000200_coordination_assignments.sql` only if it has not already been applied,
then `20261021000300_coordination_followups.sql`. Do not edit published 001/002.
Migration 003 adds repository coverage, package screening/mapping provenance and an
immutable per-run catalog snapshot. Migration 003 is UTF-8 **without BOM** and adds
nullable `crawl_jobs.screen_assignment_id` to locate an exact D2 batch. Legacy rows
may be NULL. Compare the actual schema before applying anything; this workflow
never applies migrations automatically or enables workers implicitly.
The implementation does not connect to production to discover migration history. Tools check columns, private table RLS and
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

#### Windows / Google Drive for desktop troubleshooting

An alias such as `reposeer:` backed by a mounted G: folder uses rclone's **local**
backend. Coordination commands always pass `--local-no-preallocate`, including
read-back/downloads, so correctness does not depend on a shell setting. This option
also works alongside a directly authenticated Drive remote. For manual rclone
commands in Windows CMD, use these settings in each participating terminal:

```cmd
set "REPOSEER_DRIVE_REMOTE=reposeer:"
set "RCLONE_LOCAL_NO_PREALLOCATE=true"
set "RCLONE_EXCLUDE="
```

The preallocation defect was reproduced with generated bytes in an isolated G:
workspace directory: 572 bytes were reported as 1,024, 2,388 as 2,560, and 510 as
512; `copyto` failed with exit 1. All three passed with preallocation disabled.
The historical C1 failure cannot be attributed conclusively without its original
stderr: the exact read-only version/access/listing checks now succeed and its
remote assignment directory is empty. Creating that directory alone is not a fix.

Coordination transfers handle only **undeclared** `desktop.ini` (case insensitive,
at any depth) as OS metadata, after path/link validation. These files are excluded
from new seals, downloads, inbox enumeration and immutable bundle comparisons.
If an older CHECKSUMS list explicitly declares one, it is retained and must match
its checksum and size, just like every other payload. READY and CHECKSUMS remain
authoritative; other unexpected files, missing files and modified declared files
fail verification. Existing bundles are never rewritten or re-signed. Use
`Drive.download()` followed by `verify_bundle()` for sealed bundle downloads.
Coordination subprocesses remove inherited filename/size/age/depth/hash filters
(including `RCLONE_EXCLUDE`), so machine settings cannot hide unexpected payloads.
Manual rclone commands retain their own environment; avoid broad excludes.

Errors retain `error_code` and include safe `rclone_stage` / `rclone_exit_code`
fields when applicable, without subprocess output, provider messages or secrets:

```json
{"error_code":"rclone_operation_failed","rclone_stage":"immutable_upload","rclone_exit_code":1}
```

Stages identify version, availability, listing, directory_creation,
immutable_upload (or mutable catalog upload), read_back, checksum_verification and
notification_removal. Only listing exit 3 is treated as directory absence; upload
and read-back failures are never treated as missing directories. Exit 5 indicates
a temporary error; exit 2 means usage/configuration and 7 is fatal. Other codes
need operator diagnosis. Rclone performs bounded retries (2 high/low level attempts);
timeouts and launch failures also report their stage. Ingestion retains notices
on transport failure, including timeout, and reports these same diagnostics.
Permanent errors remain visible; repeated operator retries do not fix permissions
or conflicting bytes. See [rclone exit codes](https://rclone.org/docs/#exit-code)
and [local preallocation](https://rclone.org/local/#local-no-preallocate).

`Drive.put()` creates the nested parent, uses `--immutable --ignore-existing`,
and verifies actual SHA-256 and size by downloading the destination into local
TEMP. An existing matching file succeeds without another upload. Conflicting or
partial destination bytes fail without repair, overwrite or deletion. Run a single
coordinator writer: the listing is advisory, not a distributed filesystem lock.
A read-only access preflight before claiming cannot establish write permission;
there is deliberately no test upload or open Registry transaction during transfers.

After a failed dispatch, inspect `.pending/<job_id>.json` and the remote directory,
then repeat the original dispatch with the **same output directory only while its
journal exists**. Recovery validates the current claim before publication and again
after checksum verification, then removes the journal. It never claims another job
or increments attempts on this path. If no journal exists, ordinary dispatch can
claim new work. Never delete journals, change tokens, reset jobs, bypass an expired
lease, or remove remote partial data to make publication pass. A stale claim or
checksum conflict needs inspection; keep all evidence. Verify an uploaded assignment
by comparing `Drive.remote_fingerprint(remote_name)` with `fingerprint(local_path)`.
Worker dry-run validates the assignment without a Registry DSN; ingestion independently
rechecks the live claim before accepting any canonical artifacts.

##### Recover the existing C1 publication (coordinator only)

No production publication was executed while implementing this workflow. In the
coordinator CMD terminal, with its existing Registry credential, inspect and recover
only the retained journal below. The final assignment and claim token are unchanged:

```cmd
set "JOB_ID=426f38b1-096b-453c-b655-14eaaff43696"
set "CLAIM_TOKEN=454a399a-e02d-4a4b-9f69-040d53e625a1"
set "OUT_DIR=C:\RepoSeerData\coordination\assignments\C1"
type "%OUT_DIR%\.pending\%JOB_ID%.json"
rclone lsjson "reposeer:crawl/assignments/C1/%JOB_ID%" --files-only
if exist "%OUT_DIR%\.pending\%JOB_ID%.json" uv run --frozen python tools/coordination/dispatcher.py dispatch-crawl --worker C1 --limit 1 --out-dir "%OUT_DIR%" --lease-hours 48
```

Require `recovered_assignments` before asking C1 to run `work`. Publication checks the
current claim before upload and after SHA-256 read-back, and removes `.pending` only
on success. The guard prevents ordinary dispatch when this journal is already gone.
Do not run another dispatcher concurrently. Stop for a stale claim, expired lease,
conflicting bytes or a transport error; preserve the journal and inspect its safe
stage/exit diagnostics. Do not reset or create another job for this recovery.

#### D1: discover names, never crawl

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

#### D2: screen current source metadata

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

#### C1-C7: one sequential collector per machine

Coordinator assigns one repository job per worker for the normal `work` command,
without reusing live claims (the dispatcher still supports a limit up to 10):

```powershell
uv run python tools/coordination/dispatcher.py dispatch-crawl --worker C1 --limit 1 --out-dir data/coordination/assignments/C1 --lease-hours 48
```

Crawl assignments are immutable `crawl/assignments/Cn/<job_id>/<claim_token>.json`.
The coordinator commits its claim and pending journal, validates the claim, uploads
outside any DB transaction, then revalidates it in a short transaction before
removing the journal. No mutable remote pointer can overwrite a newer claim.
If the claim changes during upload, the command fails and retains the journal;
the old token-specific remote file is orphaned, not a newly authorized assignment.
Only distribute paths from a successful dispatch. Ingest rechecks the live DB claim.
Disabled workers cannot receive new or recovered publications.

Assignments contain up to 100 linked PyPI targets, fixed bounded collector options,
expected repository, token and checksum. If a repository has over 100 packages,
`package_scope_truncated=true`; unassigned/unverified packages stay candidates.

Members run `worker.py work --worker C1`; it retrieves its own signed assignment
and reuses `run()` for collection, immutable upload and notification publication.
It skips expired assignments and matching READY submissions with a validated pending
or processed notification. Saved assignment bytes are checked against CHECKSUMS,
and CHECKSUMS against READY. These are submission evidence, not Registry acceptance.
A READY upload missing its notice is downloaded and verified if necessary, then
resubmitted with the original integration run; it is never recollected automatically.

Multiple eligible assignments, including multiple unexpired tokens for one job, fail
with `ambiguous_assignments_use_job_id_and_claim_token`. Do not choose by filename or
lease recency. After checking the Registry, the coordinator can supply exact IDs:

```cmd
uv run --frozen python tools/coordination/worker.py work --worker C1 --job-id JOB_UUID --claim-token CLAIM_UUID
```

`--job-id` alone works only if it leaves one eligible token. `--claim-token` requires
`--job-id`. `--dry-run` validates selection without collection, upload or local work
files. `--data-root` overrides the physical-disk default
`C:\RepoSeerData\coordination\work\C1` (Linux/macOS: `~/.local/share/reposeer/work/C1`).
Keep the same root when retrying; when switching from `run`, pass its existing
`--data-root`. Do not put workspaces/checkpoints on a Drive mount.
The worker has no Registry credentials and cannot detect a revoked but unexpired
single claim from immutable Drive files alone. It never invents a replacement token;
Central Ingestion always validates the current claim. Conflicts fail without overwrites.
The existing `run --assignment ... --data-root ...` CLI remains available for diagnosis.

The data-dir is exactly `<data-root>/<job_id>/<claim_token>`. The subprocess uses an argument
array and existing `collect-dataset` flags. No arbitrary command/URL is executed.
A child exit 1 with a genuine partial manifest and raw observations is submitted;
its data is retained. A missing manifest creates a local failure receipt and fails,
never a fabricated integration run. Report that failure to the coordinator; it has
no fake successful READY run. Logs are redacted and retained outside the bundle.

Retry the same command to upload saved READY data without collecting again. Local
files/checkpoints are never deleted automatically. An unfinished T-020 manifest
requires operator inspection and existing child recovery commands: T-020 has no
integration resume engine. A reissued claim automatically gets a new token directory beneath the same data-root;
both attempts remain separate. For example, attempts `J/T1/` and `J/T2/` retain their own
ASSIGNMENT, targets, manifests, raw, processed, checksum, READY and receipt. Logs stay
outside bundles under `logs/J/T1.log` and `logs/J/T2.log`.
Use the default 48-hour lease, longer than expected collection and upload time.
Use a short local disk data-root (for example `C:/RepoSeerData/C1`) if Drive Stream's
Windows path-length limit rejects the two UUID segments plus artifact names.
Spaces in paths are supported by quoting and argument arrays; no shell is used.

##### Activity coverage and follow-up scopes

| Scope | max_pages/group | max_records/group | Next automatic scope |
|---|---:|---:|---|
| initial | 1 | 30 | followup:1 only when GitHub budget_limited |
| followup:1 | 2 | 100 | followup:2 only when GitHub budget_limited |
| followup:2 | 4 | 300 | none |

External options remain hackernews / 1 page / 1 record / 1 candidate. Assignments
accept only these exact scope/options pairs; workers derive argv from validated
options. Initial scopes precede followups at equal priority. The default five
GitHub groups are unchanged.

Coverage comes from actual per-group manifest entries, preserving status,
missing_reason, nullable pages_fetched/records_stored and reason codes. Registry
statuses are unknown, budget_limited, bounded_no_budget_exhaustion or collection_failed.
A completed job means its bounded scope finished. Even bounded_no_budget_exhaustion
is not full historical coverage. External-only partial runs do not create followups;
unverified mapping/metadata retains bounded retry/failure behavior.

Followups recollect the first pages with larger budgets, consuming repeated API
requests. They are **not cursor resume**. New raw observations keep their own IDs
and timestamps. After followup:2, budget_limited remains visible with no further
automatic job. Full cursor recovery is a separate task.

##### Packages discovered after a repository was collected

A successful screening import records last_screening_run_id, then attempts reuse
only for its new candidate packages (at most 100), after committing the import.
The coordinator verifies screening bundle SHA and saved PyPI JSON, accepted canonical
GitHub raw checksum/numeric ID, the existing resolver and RepositoryScopeEvidence.
Accepted evidence is considered newest to oldest in bounded SQL batches. It uses
the newest usable checksummed GitHub repository observation with the exact numeric
ID, falling back when a newer accepted activity-only run lacks eligible metadata.
Checksum/size errors, malformed evidence or conflicting identity stop verification;
they are never bypassed to promote from an older run. Legacy runs without catalog
snapshots remain eligible for evidence validation. Per-run caching avoids repeated
file downloads for several packages. No raw activity dataset is downloaded here.
It records the selected run, source paths/hashes/observed times before promotion;
this is current observation scope, with historical_validity still unverified.
It never calls GitHub again or requeues a completed repository job for this check.
Failures remain candidates with reason codes. Existing records with no screening
reference report screening_evidence_missing; no provenance is invented.

```powershell
uv run python tools/coordination/dispatcher.py verify-pending --limit 100 --dry-run
uv run python tools/coordination/dispatcher.py verify-pending --limit 100 --apply
```

The default is dry-run. Missing/corrupt evidence and numeric identity collisions
are reported without promotion or automatic row merging. Review each result's
reason_code; restore the original immutable evidence or screen again through an
explicit operator workflow rather than editing hashes to force verification.

#### Coordinator: ingest, audit and catalog

Use local C: TEMP for Parquet work. Workspace files are verified downloads; they
may be large. No mutable DuckDB is opened on Drive Stream.

```powershell
uv run python tools/coordination/ingest.py run --remote reposeer: --workspace data/coordination/central --dry-run
uv run python tools/coordination/ingest.py run --remote reposeer: --workspace data/coordination/central --limit 25
uv run python tools/coordination/ingest.py rebuild-catalog --remote reposeer: --dry-run
uv run python tools/coordination/ingest.py rebuild-catalog --remote reposeer:
uv run python tools/coordination/dispatcher.py status --json
uv run python tools/coordination/dispatcher.py requeue-expired --pending-dir data/coordination --dry-run
# Only after reviewing current READY, pending assignments/uploads and worker state:
uv run python tools/coordination/dispatcher.py requeue-expired --pending-dir data/coordination --apply
```

`--dry-run` makes no DB or Drive writes; worker dry-run also creates no work files.
After verifying its immutable bundle upload (READY last), each crawler publishes a
small immutable `crawl/inbox/Cn/<job_id>/<integration_run_id>.json` notification.
It binds schema version, worker/job/run, exact submission prefix, READY payload SHA
and the immutable READY timestamp. Same-claim retries reuse both bundle and notice
without collecting again. No credentials or local logs enter the notification.

Normal ingest lists pending inboxes for C1-C7 only, at most --limit notices per call;
it never recursively scans crawl/submissions. It downloads only referenced READY
runs. Accepted replay checks DB identity/SHA without redownloading old bundles.
After DB commit, only the notification is copy/verified then removed from inbox into
`crawl/inbox-processed/...`. Invalid notices go to `crawl/inbox-quarantine/...` with
an explicit error and local reason under workspace/quarantine. Transport/DB failures
leave notices pending, including failures after acceptance but before archiving.
Retrying is idempotent; raw submissions and canonical bundles are never deleted.

For legacy uploads lacking notifications, run explicit bounded backfill:

```powershell
uv run python tools/coordination/ingest.py scan-legacy --remote reposeer: --workspace data/coordination/central --limit 25 --dry-run
uv run python tools/coordination/ingest.py scan-legacy --remote reposeer: --workspace data/coordination/central --limit 25 --apply
```

Backfill walks job/run directories explicitly and stops after the requested number
of eligible READY runs. It skips already indexed runs and existing pending notices,
so repeated batches progress. It also validates and backfills missing SQL catalog
snapshots for legacy accepted runs, without changing original bytes. No normal ingest
cycle triggers this scan. Catalog rebuild fails explicitly until legacy accepted
rows have snapshots, instead of silently omitting them.

To retry a quarantined notification after correcting the underlying claim/evidence
issue, inspect its local reason first, then copy its unchanged JSON back to the
same inbox path. Example (only after review; stale claims require a new assignment):

```powershell
$runId = '<integration-run-UUID>'
rclone copyto "reposeer:crawl/inbox-quarantine/C1/$job/$runId.json" "reposeer:crawl/inbox/C1/$job/$runId.json" --immutable
```

A dry-run never uploads, archives notifications, writes registry rows or rebuilds
catalog. It may read metadata and return pending/rejected diagnostics.
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
Previous accepted notifications are recognized by READY checksum and DB
artifact path; downstream authority is the coordinator-owned canonical copy.

Artifact acceptance differs from job collection. A partial run ends the bounded
initial repository job only when at least one assigned package is independently
verified against current PyPI/GitHub raw evidence and repository metadata was
actually stored. Activity budget_exhausted remains partial in run/coverage; there
is no promise of complete history. Only individually verified package IDs are
promoted. Unverified mapping/metadata runs are retained for audit and the job retries
with bounded backoff, then fails at max_attempts. No abandonment label is produced.

Ingestion has three phases: read-only preflight, immutable canonical upload/read-back
without an open DB transaction, then short locked acceptance revalidating claim,
lease, targets, worker, evidence and numeric identity. A crash or stale claim after
transfer can leave an orphan canonical directory; it cannot mark the DB accepted.
Replay reuses matching immutable bytes and rejects conflicting checksums.

Acceptance stores immutable catalog metadata in crawl_runs: run/job/worker, expected
repository, verified numeric ID, assignment target snapshot, overall status,
canonical path/SHA and ingestion timestamp. The catalog uses this SQL metadata only,
not current package lists or downloaded receipts/checksums from all accepted runs.
Normal ingest does not rebuild it. Invoke rebuild-catalog separately, or explicitly
add --refresh-catalog to ingest run for one refresh after the batch.

The explicit-schema Parquet is built atomically on local TEMP and published as
`canonical/catalog/snapshots/<sha>.parquet`, then a verified compatibility alias
`canonical/catalog/ingestion_index.parquet` and `active.json`. Consumers resolve
active.json's immutable relative_path and verify SHA. Failed publication leaves
accepted runs safe and may leave an older active snapshot; rerun rebuild-catalog.
One dedicated autocommit PostgreSQL session holds pg_try_advisory_lock across
coordinator ingestion/catalog publication, released in finally. No long-running
transaction or row lock spans transfers. Operate one coordinator writer; do not
overlap processes. No raw/processed concatenation or features are built here.

Requeue is dry-run by default. It queries expired claims first; no expired claims
means no Drive inspection. Crawler checks are confined to
`crawl/submissions/<claimed_by>/<job_id>/`; D2 checks use only
`screening/submissions/D2/<screen_assignment_id>/`. There are no global recursive
submission scans. D2 dispatch records the batch ID alongside each claim. Legacy
screen jobs with a NULL batch ID are protected for manual resolution, not reclaimed
on an assumption that no upload exists.

A matching current job/token/worker/checksum or local pending journal protects the
claim. Clearly identified uploads from older tokens do not block it forever.
Incomplete/unknown identity protects the affected job; unreadable Drive or malformed
assignment preflight aborts without DB changes. Remote reads finish before any row
locks. Apply re-reads and locks each eligible job, checking that its expired lease,
worker/token, batch and assignment still match; concurrent state changes are skipped.
Only a successfully invalidated claim has its batch/claim fields cleared. Attempts,
artifacts and journals are retained, with existing bounded backoff/max-attempt rules.

This lease protocol has no worker API: an offline worker may still upload after
expiry. A reclaimed/stale token must not be accepted as current, even if its raw
bundle later arrives. Resolve protected claims/pending journals by inspection;
do not delete evidence or manually mark collection successful.

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

#### Offline verification and production checklist

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
privileges. Never point this test at Supabase. It applies the three migrations only
there, removes only its own UUID rows and checks competing SKIP LOCKED transactions.
Without that DSN this test is skipped; offline tests cannot establish live SQL/ACL
compatibility or provider quotas.

Before production: review/apply 002 if missing, then 003; verify private schema/RLS/indexes;
secure coordinator DSN; configure restricted Drive OAuth separately; test dry-run;
activate named slots explicitly; run one small discovery/screen/crawl/ingest batch;
verify SHA read-back and catalog pointer; inspect partial/failed counts; then scale.
No production DB/Drive state, worker activation or real assignments are changed by
implementing/testing this task. A current PyPI snapshot is not historical evidence;
a pilot's 30% verified out of 20 samples is not a population verification rate.

References: [rclone copy](https://rclone.org/commands/rclone_copy/),
[rclone exit codes](https://rclone.org/docs/#exit-code),
[psycopg transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html).
