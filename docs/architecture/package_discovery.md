# T-020A PyPI package discovery and sampling

T-020A creates an evidenced input cohort before T-020 collection. Index presence
only means a valid name was observed in this sampling source. It supplies no
maintenance label, GitHub mapping, release history or historical availability.

## Entry points and sources

`PackageDiscoveryPipeline().run(PackageDiscoveryOptions(...))` and
`reposeer discover-packages` share one frame, sampling and export implementation.
The pipeline accepts an injected `PyPIIndexClient` and UTC clock. An injected
index client remains caller-owned; its borrowed HTTPX session is never closed.

| Source | Acquisition | Recorded frame |
| --- | --- | --- |
| `pypi-simple` | One bounded GET of the official root index, with bounded transient retries | Observed PyPI index |
| `snapshot` | Check JSON sidecar, byte count, SHA-256, UTF-8 and complete HTML; no HTTP | Original observed index, imported under the selected root |
| `file` | Read UTF-8 names or `pypi:` IDs one per line; no HTTP | User-supplied subset, with original byte SHA-256 |

Only explicit `--exclude-file` entries are excluded. Blank/full-comment lines are
ignored; inline comments, URLs and malformed names fail with a line number,
without echoing input. Equivalent PEP503 names share the existing
`normalize_pypi_name` and `package_id` helpers. Exclusion-file hashes describe
original bytes; excluded-name count is distinct from the intersection with the
candidate frame. Unresolved future GitHub mappings do not remove sampled names.

## Acquisition and immutable snapshots

The fixed endpoint is `https://pypi.org/simple/` with Accept
`application/vnd.pypi.simple.v1+html`. HTML is deliberately used for standard-library
incremental parsing. [PyPI Index API](https://docs.pypi.org/api/index-api/) documents
the root response and `X-PyPI-Last-Serial`; the
[Simple Repository API](https://packaging.python.org/en/latest/specifications/simple-repository-api/)
is the living specification. PyPI recommends JSON for new integrations; this
bounded HTML enumerator avoids an additional streaming JSON dependency.

HTTPX streams decoded bytes in 64 KiB chunks into a temporary binary file while
computing SHA-256 and validating UTF-8/HTML structure. The decoded byte budget is
256 MiB by default, adjustable from 1 byte through 512 MiB. Content-Length and
compressed transfer size do not replace the decoded budget. Only HTML media types
are accepted. All redirects are rejected. Session auth, cookies, default query params and other session
headers are stripped; credentials are never sent to PyPI by this client.

Defaults are 20-second connect/write/pool timeouts, 120 seconds per stalled read,
three total attempts, exponential backoff capped at 10 seconds, and a total wait
budget of 30 seconds. Transient network errors and 429/500/502/503/504 may retry.
A valid Retry-After is respected when it fits the remaining wait budget; a larger
cooldown fails explicitly. Arbitrary 4xx are not retried. Every retry starts a new
file. Logs contain host, attempt, status, decoded bytes and duration, not bodies.

After complete decoding, closing HTML/body validation, flush and fsync, bytes are
published as `registry/snapshots/<sha256>.html`, with a version-1 JSON sidecar.
SHA-256 describes the decoded response bytes, not reserialized HTML. The sidecar
stores actual source URL, retrieval UTC time, content type and optional serial,
ETag and Last-Modified. Missing headers stay null; malformed serials fail.
Existing equal-hash bytes and valid metadata are reused without changing original
provenance. Conflicting existing bytes fail rather than being overwritten.
A snapshot is usable only with a valid sidecar. Checksum verification establishes
stored-byte consistency, not an independent signature of origin or historical proof.
Retain original file-mode input and exclusion files for independent replay. A crash between the two file
publications can leave an incomplete pair, never a completed sampling run.

The incremental HTMLParser accepts root project links on the official HTTPS
origin. Href and displayed name must normalize to the same valid name. Foreign,
credentialed, navigation, query/fragment and malformed links are rejected/countable.
UTF-8 and tags may span chunks. HTML/body closing structure is required; malformed
or truncated documents fail. Buffered HTML tokens/anchor text have a 1,048,576-character defensive bound;
manual-file rows have a 1 MiB byte bound. Counts record anchors, valid entries,
invalid entries and normalized duplicates separately.

## Frame and exact algorithm

A temporary SQLite database in system TEMP contains:

```sql
CREATE TABLE candidates (canonical_name TEXT PRIMARY KEY, priority BLOB NOT NULL);
CREATE TABLE exclusions (canonical_name TEXT PRIMARY KEY);
```

At most 1,000 insertion rows are buffered in Python. SQLite deduplicates through
INSERT OR IGNORE, uses a bounded page cache and file-backed temporary sorting.
The database is scratch, not production state, and is closed before cleanup on
success or failure. All names are never collected in a Python list or set.
Only the selected sample (at most 10,000 names) is retained for export.

Algorithm version: `sha256_hash_priority_v1`. For each canonical name:

```python
priority = hashlib.sha256(
    f"reposeer:pypi-sample:v1:{seed}:{canonical_name}".encode("utf-8")
).digest()
```

Select the smallest priorities, without replacement, breaking ties by canonical
name ascending. SQLite excludes explicit names before ORDER BY priority and
LIMIT. Input order and duplicate occurrences cannot affect rank. Seed is an
integer, including negative values; default 42. Exact counts include valid entry
occurrences, unique candidates, normalized duplicates, intersecting exclusions,
eligible unique frame and selected count. An insufficient frame fails explicitly;
requested count is never silently reduced.

This is a deterministic pseudorandom sample of eligible observed names. It is not
a claim of statistical representation of all active Python projects. Popularity,
activity, stars, releases and abandonment assessments are not selection filters.

## Registry and publication

```text
ROOT/registry/
  snapshots/<sha256>.html
  snapshots/<sha256>.json
  runs/<sampling-UUID>/
    manifest.json
    package_candidates.csv
    targets.txt
    batches/batch_001.txt
    batches/batch_002.txt
```

CSV has the exact columns:

```csv
sampling_run_id,sample_rank,package_id,package_name,batch_id,source_kind,eligibility_status,mapping_status,collection_status
```

Rank starts at 1. Names/IDs are canonical. Initial states are `name_valid_only`,
`not_checked`, `not_started`; source kind distinguishes live index,
`verified_snapshot`, and `user_supplied_file`. No repository is guessed.
Targets and batches are UTF-8 with LF endings. Batch size is 1-100 (default 50),
count is 1-10,000. Batch IDs use minimum three-digit padding; for over 999 batches,
use manifest array order rather than lexical filename sorting. Ordered batch
concatenation equals targets.txt exactly; batches are nonempty and disjoint.

Outputs are written/fsynced under a unique hidden staging directory outside runs.
Actual file hashes, row counts, selected IDs and batch contents are checked before
a complete manifest is written and the directory is renamed to its fresh UUID.
Old runs are not overwritten. Incomplete staging is not a completed UUID run.
Failure cleans scratch/staging without deleting successful historical artifacts.
The manifest includes source evidence, seed, algorithm, frame/exclusion counts,
portable root-relative artifact paths/hashes and limitations. It does not hash
itself. File input has null snapshot fields and its own source-byte hash.

Replay retains original snapshot retrieval time and serial, with
`acquisition_mode=offline_replay`. Replaying into another root imports the verified
snapshot pair. Same frame, seed, count, exclusions and batch size reproduce target
and batch bytes. CSV UUID and manifest run timestamps legitimately change.

## CLI and T-020 handoff

```powershell
uv run reposeer discover-packages --source pypi-simple --count 1000 --seed 20261020 --batch-size 50 --data-dir data
uv run reposeer discover-packages --source snapshot --snapshot "data/registry/snapshots/<sha256>.html" --count 1000 --seed 20261020 --batch-size 50 --data-dir data
uv run reposeer discover-packages --source file --input-file packages.txt --exclude-file exclusions.txt --count 3 --batch-size 2 --data-dir data
uv run reposeer collect-dataset --targets-file "data/registry/runs/<sampling-UUID>/batches/batch_001.txt" --data-dir data --mode live
```

Exit codes: 0 complete sample; 1 acquisition/parsing/IO/insufficient-frame failure;
2 invalid arguments or incompatible source flags, validated before network.
The command prints a short summary and artifact paths, not the full index.
Never pass aggregate targets.txt to T-020 when it contains more than 100 targets.
Discovery does not trigger T-020, fetch per-package JSON, query GitHub, install
packages, crawl articles or call an LLM. Registry artifacts are not T-011 raw
records and are excluded from unchanged T-019 processing inputs and schemas.

## Operations and verification

Use an isolated data root for demos. Keep enough disk space for decoded index,
source temporary file and SQLite frame/sort workspace. Registry outputs under the
normal data root are Git-ignored. Use one writer per root, close file/SQLite handles
before rename/cleanup on Windows, and keep system TEMP on a local disk when using
Google Drive Stream. Filesystem atomicity and Drive sync are not a distributed
transaction or a concurrent-writer guarantee.

```powershell
uv run --frozen pytest -q tests/integration/test_package_discovery.py
uv run --frozen pytest -q tests/integration/test_collection_pipeline.py
uv run --frozen pytest -q -m "not smoke"
uv run --frozen ruff check src tests scripts
```

Tests are offline, including a generated 100,000-project streaming sanity frame,
HTTP retry/size/interruption failures, checksum/provenance replay, deterministic
ranking, exclusions, staged publication failure and exact T-020 parser handoff.
Live verification is separate and opt-in; use count=10 and batch-size=5, then replay
the actual snapshot offline. A current index does not establish a historical
cohort, package health or verified package/repository mappings. T-020 must still
run separately per batch with its own evidence, budgets and limitations.
