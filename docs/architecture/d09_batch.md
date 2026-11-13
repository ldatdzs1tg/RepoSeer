# D09 historical batch

Run from the RepoSeer root in the project Python environment.

## Run from the Drive screening list

```powershell
.\.venv\Scripts\python.exe -m scripts.run_d09_drive --all --continuous
```

The runner validates the Drive screening CSV against its metadata and freezes all package IDs. Use `--plan-only` to validate and freeze the list before making provider requests. Omit `--all` to run a pilot of approximately 1,000 packages. The pilot prioritizes screening candidates and keeps each current repository group together. Current screening names serve only as grouping hints. Historical mapping and split groups still use evidence available at cutoff and stable numeric GitHub IDs.

The full run writes to `data/d09/drive-full-v1`. Each pass processes up to five new batches and attempts up to 30 new GH Archive downloads. `--continuous` starts the next pass automatically and stops if repeated passes make no progress. Omit it to run one pass at a time. Run the same command again after an interruption. `--max-batches` and `--max-downloads` change these limits without changing membership. Use `--screening-dir` if the mounted screening folder is elsewhere. Run `--retry-unverified` separately to retry prior PyPI or archive verification failures.

Large registry lookups send bounded batches of frozen package IDs. When the public ClickHouse quota is reached, the runner saves the retry time in `provider-quota.json`. Continuous mode waits and resumes from the checked query receipts. One-pass mode returns control to the terminal so the same command can be rerun after the recorded time.

To build the review queue across all batches before downloading raw archives, use `--all --continuous --collect-only` with the same output directory. Progress goes to `collection-summary.json`. Existing verification and split reports stay available. Run again without `--collect-only` to resume original archive verification. Interrupted downloads from the current verifier resume by byte range and only produce evidence after the full pinned object passes size and checksum validation.

Full verification shares original archive proofs across batches. Every newly scanned object retains matching original lines for all repository hints in the frozen screening and the already collected historical mappings, including repositories in future batches. The shared store is `archive-cache/`. Hints select extra lines to retain and do not establish mapping identity or historical availability. Before reuse, the verifier checks the retained bytes, original revision and inventory receipt, and applies the same numeric repository ID, cutoff and lookback rules. Each batch receives its own matching raw lines and source receipts, so its proofs remain portable. Existing per-batch proofs and unfinished downloads remain usable. Restart a running process to load this update, using the same command and output directory.

Proof receipts record the digest of the full set of extraction hints and the names actually found. A copied subset cannot claim that it covers the entire extraction scope. The runner indexes retained names once per pass and replays only relevant proofs. Historical extraction hints are cached against each batch's collection revision so repeated passes do not rescan the whole review queue.

Within a batch, the verifier also replays proofs across all compatible package-cutoffs and prioritizes new archives by pending rows covered per byte remaining. Up to four distinct archives are checked concurrently by default. Set `--archive-workers` from 1 to 16 to adjust concurrency. The download budget still applies to the whole pass. This reduces repeated network work, but a full run can still require many large original objects. A mapping proof does not establish complete feature coverage or verify an outcome label.

The runner uses the public PyPI and GH Archive mirrors for collection, then verifies candidate evidence against the original providers. Each batch retains its frozen roster, raw receipts, `review.jsonl`, mapping verification and official-status leads. Keep the run directory local while processing. `summary.json` links the reviewed output directories and `split-audit.json` reports the aggregate time and repository-group checks. The pilot and full-run directories have separate checkpoints.

When the team has independently reviewed records for this cohort, place them in `data/d09/drive-full-v1/reviews.jsonl` or pass `--reviews` and rerun. Use one `ReviewedD09Target` per line. Preserve the verified availability dates from the evidence. Missing reviews stay in the eligibility audit. The split audit is available before labels arrive and checks class presence independently of sample-size sufficiency for Model A.

## Check readiness before scaling

After the first completed batches, audit the retained evidence and the actual feature/label builder output before treating further mapping downloads as progress toward training:

```powershell
.\.venv\Scripts\python.exe -m scripts.audit_d09_readiness --output-dir data/d09/readiness-audit-v1
```

The auditor freezes the completed batch set, excludes batches waiting for downloads, checks cohort and source hashes, rechecks retained PyPI and original-event proofs, and runs the reviewed builder and chronological split auditor. It makes no provider requests and writes only to a new audit directory, so the collection can keep running. Use a fresh output directory for another audit. `--reviews` is repeatable to inspect existing review catalogs; only matching package-cutoffs enter the audited cohort, and duplicate or future-dated matching reviews fail validation. With no explicit file, the auditor uses the run's `reviews.jsonl` if it exists.

Read `report.md`, `summary.json` and `split-audit.json`. `eligibility.jsonl` preserves every audited package-cutoff. `review-sample.jsonl` contains a deterministic readiness probe of five mapped rows per split, selected without outcome-based ranking. `official-leads.jsonl` preserves the capped search leads and source paths. Optional `--source-checks` records checksum-checked source observations separately; these observations cannot assign supervised labels. `input-manifest.json` captures source hashes and rejects inputs that change during the audit.

A mapping proof is not a feature snapshot or an outcome label. Original-event excerpts do not certify a complete activity census, and keyword hits can describe a dependency or API change. The current D09 policy also places actual official verification and complete-follow-up verification times into label availability. Labels first verified in 2026 miss the existing 2022 fit and 2024 selection deadlines. Resolve that evaluation design using genuinely pre-existing verified labels or a prospective holdout before claiming training readiness. Do not backdate new reviews. Class presence alone is not sample-size or calibration approval.

## Run the historical PyPI pilot

```powershell
python -m scripts.collect_d09_batch --sample-size 300 --shard-size 25 --output-dir data/d09/historical-review-300-v1
python -m scripts.audit_d09_archive --output-dir data/d09/archive-inventory-2019-2025
```

The collector freezes a seeded sample of PyPI projects with an archived distribution before the last 2024 cutoff. Each project enters at the first quarterly cutoff after its first observed upload. Sampling uses the registry, with the same seed across runs, before looking at GitHub activity or outcomes. The sample describes the archive population and its subsequent eligibility filters. [ClickPy projects data](https://github.com/ClickHouse/clickpy/blob/main/ClickHouse.md#projects-table) · [PyPI distribution metadata](https://docs.pypi.org/api/bigquery/)

Repeat the collection command with `--resume` and identical parameters after an interruption. Each shard retains SQL, retrieved response bytes, timestamps and SHA-256. A changed sample size or shard size requires a new run directory. The public mirror exposes PyPI uploads to millisecond precision. The collector excludes boundary-millisecond uploads to keep mapping candidates on the safe side of the cutoff.

`cohort.json` freezes membership. Each shard's `review.jsonl` records candidate mappings and observed horizon releases. Its `sources/official-leads-*.jsonl` files contain capped keyword matches, including false positives. The collector emits one review row per eligible package and cutoff, including unresolved rows.

The archive audit lists the current revisions of hourly objects from 2019 through 2025. It records missing files and revisions created after a window's end. Use a pinned generation and its digest to verify individual historical records. A complete object inventory still requires verification of event coverage and collection scope before claiming complete CORE coverage. [GH Archive](https://www.gharchive.org/) · [Cloud Storage object metadata](https://docs.cloud.google.com/storage/docs/json_api/v1/objects)

## Cross-check candidate sources

```powershell
python -m scripts.verify_d09_evidence --batch-dir data/d09/historical-review-300-v1 --inventory-dir data/d09/archive-inventory-2019-2025 --output-dir data/d09/verification-300-v1 --max-downloads 6
```

The verifier checks each single-ID candidate against the exact PyPI version endpoint. It compares the declared artifact SHA-256, upload time and explicit source link. It then uses the GH Archive mirror to locate candidate hours and matches the repo name and stable ID in an original, generation-pinned archive. It checks the inventory receipt, compressed size and MD5, and records SHA-256 for the archive and event. Both sources must have been available by cutoff. [PyPI release endpoint](https://docs.pypi.org/api/json/#get-a-release) · [Cloud Storage object retrieval](https://docs.cloud.google.com/storage/docs/json_api/v1/objects/get)

`mapping_evidence.jsonl` carries the source proofs, statuses and reasons per candidate. `mapping_sources_matched` means the two original sources agree for the package-cutoff. Package scope, mapping continuity, complete feature coverage and official outcomes remain separate review decisions. The script retains receipts and matched records. It discards downloaded gzip files after scanning.

Use `--package NAME` to select packages and `--archive-dir` to reuse locally available gzip files. Repeat with `--resume` and the same candidate selection and inventory to reuse checked responses. `--max-downloads` is a per-invocation budget and can be changed on resume. A zero budget allows local files and saved proofs only for the archive stage.

## Review and build

1. Verify package scope, the stable GitHub ID and the mapping's availability and validity at the cutoff. For example, match the historical PyPI version and artifact digest to a source URL, then corroborate the repository identity with an event in an original GH Archive object revision created by cutoff. Keep ambiguous mappings in the audit universe.
2. Record CORE coverage by group and window. Complete activity coverage spans the 12-month lookback. Complete release coverage includes the archive's first package release and the full history through cutoff. Record the source revisions and keep incomplete groups nullable.
3. Apply the same independent official-status search to the cohort. Record verified plans or events with package-scoped evidence, availability, verification and revocation. For a negative label, attest full 12-month follow-up and cite the supporting sources. Verified maintenance must also fall within the horizon. Review leads before asserting cessation.
4. Write one `ReviewedD09Target` JSON object per line. The model uses `SnapshotBuildRequest`, `PackageMapping`, `SourceProvenance` and the scoped official ledger. `followup=null` preserves unknown outcomes where follow-up could not be verified.

```powershell
python -c 'import json; from reposeer.pipelines.d09 import ReviewedD09Target; print(json.dumps(ReviewedD09Target.model_json_schema(), indent=2))'
$observationEnd = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fff'Z'")
python -m scripts.build_d09_reviewed --batch-dir data/d09/historical-review-300-v1 --reviews data/d09/reviews.jsonl --observation-end-at $observationEnd --output-dir data/d09/reviewed-300-v1
```

Omit `--reviews` to audit a collection before any verified input arrives. The builder joins strict labels one-to-one with usable snapshots on package, cutoff, repo group and horizon. `eligibility.json` preserves all rows and dispositions. `summary.json` records input hashes and class balance. The strict artifact feeds the chronological split audit after both classes are available. Store candidate evidence under `pilots/d09/` and keep `canonical/` for reviewed releases.

## Audit chronological splits

The pilot places cutoffs before 2023 in train, cutoffs from 2023 through September 2024 in validation, and the last 2024 quarterly cutoff in test. The auditor purges outcomes that overlap the next split and labels unavailable at each decision time. It reports temporal counts and a separate group-disjoint view that reserves repository groups for the latest split. A shared event ID is also reserved for the latest eligible split when provided. Counts show whether both classes are present in each split. They do not establish an adequate sample size for training or calibration.

```powershell
python -m scripts.audit_model_a_splits --d09-dir data/d09/reviewed-300-v3 `
  --fold-fit-at 2022-12-31T23:59:59.999Z --validation-start-at 2023-01-01T00:00:00.000Z `
  --selection-freeze-at 2024-09-30T23:59:59.999Z --test-start-at 2024-10-01T00:00:00.000Z `
  --test-end-at 2025-01-01T00:00:00.000Z --evaluation-as-of $observationEnd `
  --output data/d09/outcome-review-v2/split-audit-final.json
```

## Find more first-party outcome evidence

Use a JSONL queue with `package_id`, `repo_group_id`, `cutoff_at` and `horizon_end_at`
from the checked snapshots. The repository scan reads README, changelog, release
notes and status or migration documents at pinned Git revisions. It also searches
GitHub release notes, issues, pull requests and comments. The publisher scan uses
the same expanded phrases and looks for release metadata by year for queued packages.

```powershell
.\.venv\Scripts\python.exe -m scripts.discover_d09_repo_notices --targets candidate-cutoffs.jsonl `
  --output-dir data/d09/notice-scan-v1 --use-gh
.\.venv\Scripts\python.exe -m scripts.discover_d09_outcomes --targets candidate-cutoffs.jsonl `
  --output-dir data/d09/publisher-notices-v2 --headlines --max-shards 0
```

`--use-gh` uses an existing GitHub CLI login for public reads. The scan records
what it could fetch, including document and page limits. It separates material
before cutoff, in the outcome horizon and after the horizon. `leads.jsonl` retains
source paths, checksums and excerpts. Candidate dates still need verification
against the original publication and edit history. Review the package scope,
authority, effective date, any successor, and whether maintenance resumed.
An incomplete scan leaves follow-up unresolved. Approved decisions still enter
`prepare_d09_model_a` as `OutcomeReview` records with retained sources.

## Train the retrospective Model A baseline

Install the `training` extra in the environment used for fitting. Inference loads the JSON artifact and uses the existing RepoSeer runtime.

```powershell
uv pip install --python .\.venv\Scripts\python.exe -e ".[training]"
```

Use `scripts.prepare_d09_model_a` to build CORE snapshots from completed mapping batches. It checks the historical release census against publisher data and retains original files when a release is missing from the current index and `--repair-artifact-budget` allows another download. Repository activity features remain null when their history lacks verified coverage. Pass `--outcomes` with one independently reviewed `OutcomeReview` per line. Each decision references retained source files and their checksums. A negative approval also requires a complete 12-month follow-up with verified maintenance. Leads without an approval stay outside supervised fitting.

```powershell
.\.venv\Scripts\python.exe -m scripts.prepare_d09_model_a --run-dir data/d09/drive-full-v1 `
  --output-dir data/d09/model-a-retrospective-v1 --outcomes data/d09/approved-outcomes.jsonl
.\.venv\Scripts\python.exe -m scripts.train_model_a --data-dir data/d09/model-a-retrospective-v1
```

The trainer checks the prepared sources and fits on the repository-disjoint rows
from the chronological audit. It removes rows with overlapping horizons or labels
unavailable at the actual fitting and evaluation times. The default validation
window starts in January 2022 and the test window covers 2024. Each split needs
both classes after those checks.

To combine independently checked preparations, supply each directory with
`--prepared-dir`. The assembler keeps one reviewed cutoff per package and repo,
rebuilds the labels from the checked reviews, and reports the per-split gaps.
For incremental preparation, put only the new reviewed `OutcomeReview` records
in the next JSONL. `--approved-only` builds their exact package-cutoffs and
checks that each approval has a verified mapping and produces a strict row.
Reuse the project cache and use a new directory for each batch of approvals.

```powershell
.\.venv\Scripts\python.exe -m scripts.prepare_d09_model_a --run-dir data/d09/drive-full-v1 `
  --project-cache-dir data/d09/model-a-retrospective-v1 --outcomes data/d09/approved-next.jsonl `
  --approved-only --output-dir data/d09/new-approved-prep
.\.venv\Scripts\python.exe -m scripts.build_model_a_sample `
  --prepared-dir data/d09/model-a-next-v1/prepared --prepared-dir data/d09/new-approved-prep `
  --output-dir data/d09/model-a-minimum-sample-v1
.\.venv\Scripts\python.exe -m scripts.train_model_a --data-dir data/d09/model-a-minimum-sample-v1 `
  --require-minimum-sample
```

The minimum sample gate requires 20 verified rows per class and at least 20
repository groups in each split. `training-readiness.json` records any gaps and
the trainer exits with code 2 when the selected requirement fails.

On success, the timestamped run folder contains the frozen `model-a.json`, split audit, metrics with feature coverage, and a held-out `sample-snapshot.json` and `sample-inference.json`. The pilot selects cases by outcome to exercise the workflow. Its metrics describe that sample and its retrospective evaluation. Release gates keep the artifact out of production scoring. The optional `--experimental` flag returns a development score when the snapshot has usable CORE features.

```powershell
.\.venv\Scripts\python.exe -m scripts.infer_model_a --model <run-folder>/model-a.json --snapshot <run-folder>/sample-snapshot.json
```

## Analyze a cutoff feature package

Export the cutoff package from one reviewed D09 record. The export carries the snapshot, mapping and coverage sources used at the cutoff. Only official status available at the cutoff can accompany the export.

```python
from pathlib import Path

from reposeer.analysis.core import CoreFeaturePackage
from reposeer.pipelines.d09 import ReviewedD09Target

review = ReviewedD09Target.model_validate_json(review_line)
package = CoreFeaturePackage.from_review(review)
Path("core-feature-package.json").write_text(package.model_dump_json(indent=2), encoding="utf-8")
```

```powershell
.\.venv\Scripts\python.exe -m reposeer.cli.app analyze `
  --model <run-folder>/model-a.json --feature-package core-feature-package.json
```

Analyze reads the frozen artifact and emits a validated `FinalReport` as JSON. The retrospective baseline returns `insufficient_evidence` with cutoff provenance, coverage and a reason. Released artifacts need a frozen `analysis_policy` with calibration and threshold versions, ordered thresholds and the calibrated CORE feature ranges. An ENHANCED request uses CORE with a fallback reason when a CORE score is eligible. Otherwise the report records `enhanced_status="not_run"`.
