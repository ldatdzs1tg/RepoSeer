# D09 historical batch

Run from the RepoSeer root in the project Python environment.

```powershell
python -m scripts.collect_d09_batch --sample-size 300 --shard-size 25 --output-dir data/d09/historical-review-300-v1
python -m scripts.audit_d09_archive --output-dir data/d09/archive-inventory-2019-2025
```

The collector freezes a seeded sample of PyPI projects with an archived distribution before the last 2024 cutoff. Each project enters at the first quarterly cutoff after its first observed upload. Sampling uses the registry, with the same seed across runs, before looking at GitHub activity or outcomes. The sample describes the archive population and its subsequent eligibility filters. [ClickPy projects data](https://github.com/ClickHouse/clickpy/blob/main/ClickHouse.md#projects-table) · [PyPI distribution metadata](https://docs.pypi.org/api/bigquery/)

Repeat the collection command with `--resume` and identical parameters after an interruption. Each shard retains SQL, retrieved response bytes, timestamps and SHA-256. A changed sample size or shard size requires a new run directory. The public mirror exposes PyPI uploads to millisecond precision. The collector excludes boundary-millisecond uploads to keep mapping candidates on the safe side of the cutoff.

`cohort.json` freezes membership. Each shard's `review.jsonl` records candidate mappings and observed horizon releases. Its `sources/official-leads-*.jsonl` files contain capped keyword matches, including false positives. The collector emits one review row per eligible package and cutoff, including unresolved rows.

The archive audit lists the current revisions of hourly objects from 2019 through 2025. It records missing files and revisions created after a window's end. Use a pinned generation and its digest to verify individual historical records. A complete object inventory still requires verification of event coverage and collection scope before claiming complete CORE coverage. [GH Archive](https://www.gharchive.org/) · [Cloud Storage object metadata](https://docs.cloud.google.com/storage/docs/json_api/v1/objects)

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
