# D09 historical batch

Run from the RepoSeer root in the project Python environment.

## Run from the Drive screening list

```powershell
.\.venv\Scripts\python.exe -m scripts.run_d09_drive --all --continuous
```

The runner validates the Drive screening CSV against its metadata and freezes all package IDs. Use `--plan-only` to validate and freeze the list before making provider requests. Omit `--all` to run a pilot of approximately 1,000 packages. The pilot prioritizes screening candidates and keeps each current repository group together. Current screening names serve only as grouping hints. Historical mapping and split groups still use evidence available at cutoff and stable numeric GitHub IDs.

The full run writes to `data/d09/drive-full-v1`. Each pass processes up to five new batches and attempts up to 30 new GH Archive downloads. `--continuous` starts the next pass automatically and stops if repeated passes make no progress. Omit it to run one pass at a time. Run the same command again after an interruption. `--max-batches` and `--max-downloads` change these limits without changing membership. Use `--screening-dir` if the mounted screening folder is elsewhere. Run `--retry-unverified` separately to retry prior PyPI or archive verification failures.

The runner uses the public PyPI and GH Archive mirrors for collection, then verifies candidate evidence against the original providers. Each batch retains its frozen roster, raw receipts, `review.jsonl`, mapping verification and official-status leads. Keep the run directory local while processing. `summary.json` links the reviewed output directories and `split-audit.json` reports the aggregate time and repository-group checks. The pilot and full-run directories have separate checkpoints.

When the team has independently reviewed records for this cohort, place them in `data/d09/drive-full-v1/reviews.jsonl` or pass `--reviews` and rerun. Use one `ReviewedD09Target` per line. Preserve the verified availability dates from the evidence. Missing reviews stay in the eligibility audit. The split audit is available before labels arrive and checks class presence independently of sample-size sufficiency for Model A.

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
