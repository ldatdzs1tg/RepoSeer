# T-020 Collection Integration and Data Profiling

## Entrypoints and identity

`CollectionPipeline(StoragePaths(root)).run(targets, mode="live" | "stored", ...)`
is separate from the legacy GitHub-only `FullPipeline`. The public
`CollectionPipelineResult` contains integration/processing/child IDs, status,
requested/resolved counts and manifest/report paths. Constructor factories inject
PyPI, activity, discovery and crawl clients; the default live adapters share one
owned HTTP session, with HTTP cache under this storage root. Injected clients keep
their existing ownership rules. Stored mode constructs no network client.

`collect-dataset --targets-file targets.txt` accepts 1-100 entries: `pypi:<name>`,
`owner/repo`, or root HTTPS GitHub URLs. Blank/full-comment lines are ignored;
inline comments are not parsed. All targets and budgets are validated before
network. Equivalent names are deduplicated for work while each input occurrence
retains a target result. PyPI names use PEP503 canonical identities; GitHub IDs come
from actual numeric provider identity, never a name heuristic.

PyPI JSON is saved as canonical `pypi_json/dependency_metadata` observations at
`raw/pypi/packages.jsonl`. This is the established raw representation, not a new
PyPI ETL projection. Only Source/Repository/Code root URLs can propose a repository.
The existing resolver and T-017 scope verifier must accept both persisted PyPI and
GitHub evidence before any package/repository binding. Missing/conflicting evidence
stays unresolved/ambiguous. Current mapping remains repo-only and historically
unverified. Parsed JSON does not establish an HTTP body hash/status; those raw
fields remain null through the current PyPI client API.

## Live execution

1. Fetch/persist current PyPI evidence for requested packages.
2. Call `ActivityBatchService.start` once for unique repository slugs, using its
   existing metadata collection and five-group expansion. There is no preliminary
   `collect_many` metadata fetch. Canonical checkpoint context identifies the exact
   persisted metadata raw IDs; its real run ID may differ from the activity run.
3. Verify package mappings; pass the original recorded GitHub observation to
   `TechnologyDetector.detect`. Metadata recognition is not a Functional Profile.
   Missing README/manifests are declared; this integration does not fetch them
   without their collection contract.
4. Pass the same observation and detection result to `ExternalPipeline.discover`.
   Package identity is supplied only for verified mappings. The full unchanged
   search plan, upstream result/manifest, query hash, jobs, errors and projection
   state references remain in stage details.
5. Crawl only `discovery_result.collection_result.records` from that execution.
   Pass its explicit projection state and valid repository-scope evidence. No
   global append-only discovery file is substituted. No candidates means skipped
   with `no_discovery_candidates`, not a synthetic article or a crawler failure.
6. Run unchanged T-018/T-019 via `StoragePaths.processing_inputs()`, excluding PyPI
   metadata and discovery candidates. Only verified package mappings produce
   repository-bound `VerifiedTargetContext` objects.
7. Profile all seven T-019 dataset conventions and publish both reports.

T-016 plans can include provider-specific jobs outside the configured sources.
Integration status covers **explicitly requested sources**; unchanged upstream
status remains in stage details, and outside-scope providers are `not_requested`.
A requested unavailable/failed/budget-limited job still makes integration partial
or failed. Successful zero-result requested searches are `attempted_no_results`.
A direct repository covered by its verified package's external run is recorded as
`shared_repository_context` rather than counted twice.

## Stored execution and scope

Stored mode performs no PyPI/GitHub/search/web requests and writes no new raw data.
It selects saved evidence deterministically by `(retrieved_at, raw_record_id)`,
records both evidence IDs/times, and never describes that as a new live verification.
Missing saved PyPI evidence does not create a package mapping.

Three scopes are distinct:

- Requested targets and evidenced child runs belong to the collection report.
- Processing reads all eligible raw inputs under the root, including earlier runs.
- Data profiling is `storage_root_snapshot_after_run`, including earlier processed
  observations. Inserted counts and cumulative totals are separate.

Network stages are skipped with `stored_mode_no_network`. An optional `--cutoff-at`
is accepted only here, for existing T-018 diagnostics. It neither backdates current
observations nor reconstructs historical versions or cutoff-safe features.

## Persistence and recovery

The integration UUIDv4 is an orchestration identity, never a replacement for child
raw run IDs. Outputs are:

~~~text
metadata/collection_runs/integration_<UUID>.json
reports/data_quality/collection_profile_<UUID>.json
reports/data_quality/data_profile_<UUID>.json
~~~

Manifest version `1` has requested/normalized target results, mappings/evidence,
stage statuses and original child runs, actual processing metadata, report paths,
sanitized error codes and limitations. A started manifest is atomically saved before
network, then updated after stages. Root-relative POSIX references identify reports
and child manifests; `storage_root` is local location metadata.

Existing raw/child artifacts survive later failures. Activity checkpoints,
resumability and next cursors are retained; resume using `collect-activity resume`,
`discover-external --resume` or existing crawler cursor APIs. There is no new
integration resume/checkpoint engine. After child recovery, stored replay rebuilds
processed/profile outputs idempotently. Audit rows remain separate per processing
run. Single writer and per-file Parquet atomicity remain; no seven-file transaction,
locking or immutable snapshot store is claimed.

## Report semantics

Collection profiles retain per-target identity/mapping and metadata/activity/
discovery/crawl states. Sources report attempted/succeeded/partial/failed/skipped,
known emitted/stored/rejected counts, pages, pagination and candidate/content counts.
Unavailable counters remain null. A known subtotal and unknown-source list accompany
a null total when storage counts are incomplete.

Data profiles include presence, portable path, row/column counts, schema strings,
per-column null counts/rates and compatibility `stats/missing_rate`. Missing tables
have null counts; present empty tables have zero rows and undefined/null rates.
No empty Parquet is created to satisfy profiling. For timestamp-bearing tables,
minimum/maximum retrieval and latest age use an injected UTC clock. Available-time
null counts/rates are measured without synthesizing missing times.

Processing metrics retain the actual T-018 summary: input occurrences, unique raw
observations, repeated inputs, accepted/rejected/internal observations, logical
entity grouping and content revisions; duplicate-input rate uses input occurrences
as denominator. Content duplicate groups are read from that processing run's audit.
Two observations of one repository are not duplicates merely because IDs match.

External collection counts refer to integration child runs. Relevance counts/rate
refer to saved root assessments, publish the denominator, and are `not_assessed`
when none exist. `run_relevance_status` separately describes assessments in this
processing run. Query matching is not article relevance.

Illustrative report excerpts (not a claim of live verification):

~~~json
{
  "overall_status": "partial",
  "targets": [
    {"package_id": "pypi:example", "mapping_status": "verified_current"},
    {"package_id": "pypi:unknown", "repo_id": null, "mapping_status": "unresolved"}
  ],
  "sources": [
    {"source": "github_metadata", "state": "completed_with_records", "records_stored": 1},
    {"source": "rss", "state": "failed", "attempted": 0}
  ],
  "source_coverage": "not_verified",
  "historical_validity": "unverified",
  "data_profile_excerpt": {"github_activity": {"available_at_null_rate": 1.0}}
}
~~~

`reposeer profile --data-dir ROOT [--output FILE]` independently uses the same
profiler for all dataset conventions. `scripts/profile_dataset.py` delegates to that
CLI implementation. Canonical T-011 `0.1.0`, T-018 rules and all seven T-019 schemas,
keys, output hashes and portable-path metadata are unchanged. Features, labels,
abandonment judgments, model probabilities, agents and T-021 reports remain outside
this task.
