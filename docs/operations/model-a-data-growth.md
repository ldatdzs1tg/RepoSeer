# Keep growing the Model A dataset

The 20-per-class requirement is a minimum training gate. It does not limit the
collector, the sample assembler, or the number of eligible rows used by the
trainer. The `model-a-minimum-v1` directory name does not impose a size limit.

Collection and discovery can run in batches until they exhaust the frozen
cohort. Outcome review remains a separate step. A matched repository mapping or
a notice lead does not assign a label.

## Set up a PowerShell session

```powershell
$repo = 'D:\coding\HUST\DataScience\RepoSeer'
Set-Location $repo
$py = "$repo\.venv\Scripts\python.exe"
$base = "$repo\data\d09"
$root = "$base\model-a-minimum-v1"
$growth = "$base\model-a-growth-v1"
$current = "$root\sample-v9"
```

`$current` is the starting dataset for the next merge. After restarting the
terminal, set it to the latest successfully assembled sample. Keep the original
prepared directories, review files, and retained sources in place because sample
manifests reference and verify them.

## Keep collecting

Resume the existing full run and automatically continue through successive
passes:

```powershell
& $py -m scripts.run_d09_drive `
  --all --continuous `
  --output-dir "$base\drive-full-v1"
```

Each pass allows five new verification batches and 30 new archive downloads by
default. Continuous mode starts further passes, waits for recorded provider
quota resets, and stops on completion or repeated lack of progress. These are
work budgets per pass, not limits on dataset size. Interrupt with Ctrl+C and run
the same command to resume. Use one collector process per output directory.

To collect the queue across every batch before spending more time on original
archive verification, run this first, then run the command above:

```powershell
& $py -m scripts.run_d09_drive `
  --all --continuous --collect-only `
  --output-dir "$base\drive-full-v1"
```

Queue collection reports progress in `collection-summary.json`; verification
reports it in `summary.json`. Both live under `drive-full-v1`. Collection can
finish with zero supervised rows when no outcome reviews have been supplied.

## Increase throughput and stop at a chosen total

Stop the existing collector with Ctrl+C and let it exit before starting another
collector on the same directory. To resume with eight concurrent archive checks,
a larger download budget per pass, and a total limit of 100 batches:

```powershell
& $py -m scripts.run_d09_drive `
  --all --continuous `
  --output-dir "$base\drive-full-v1" `
  --archive-workers 8 --max-downloads 200 `
  --stop-after-batches 100
```

Eight workers can improve throughput when the connection and CPU have spare
capacity. Measure completed archives over comparable intervals; extra workers
do not guarantee twice the speed. The larger per-pass download budget reduces
how often the runner repeats preparation, cached-source checks, and audit work.
It does not change the download concurrency or verification requirements.

The supported archive-worker range is 1–16, with a default of 4. To try 16,
replace `--archive-workers 8` with `--archive-workers 16` in the command above.
Stop and restart the existing process to apply the setting. Compare completed
archives and compressed bytes over similar 10–15 minute intervals. If throughput
does not improve or timeouts increase, return to 8. Archive workers do not
parallelize the PyPI metadata checks.

`--stop-after-batches 100` checks the first 100 frozen batches, including batches
already processed. It continues retrying resumable archive work inside that
prefix before reporting `stopped_at_batch_limit`. The summary still reports the
full cohort's batch count. Increase or omit this flag in a later run to continue
beyond the prefix. Existing checkpoints and the frozen roster remain usable.
`--max-batches` is a separate budget for new batch attempts in each pass; in
continuous mode it resets and does not enforce a total batch limit.

For broad candidate discovery sooner, use queue collection with the same total
limit:

```powershell
& $py -m scripts.run_d09_drive `
  --all --continuous --collect-only `
  --output-dir "$base\drive-full-v1" `
  --stop-after-batches 100
```

Queue collection skips original archive verification. After reviewing promising
outcome leads, verify and prepare the selected package-cutoffs before fitting.
This puts work into candidates likely to add independent labeled rows without
requiring completion of the full archive backlog first.

At the nominal 250 packages per batch, 100 batches cover roughly 25,000 package
candidates. Training readiness depends on their independently reviewed outcomes,
complete features, and the rows left after time and repository split checks.
The project's minimum gate requires 20 rows of each label and at least 20
repository groups in every split. Counts beyond that floor remain useful; judge
the benefit of more reviewed training data using validation learning curves,
while retaining a separate test set. A batch limit is a work budget rather than
a statistical sample-size guarantee.

## Search beyond the earlier shortlists

Search full publisher descriptions for every historical package in the frozen
registry, including packages first published in 2020 or later:

```powershell
& $py -m scripts.discover_d09_outcomes `
  --run-dir "$base\drive-full-v1" `
  --output-dir "$growth\publisher-full-v1" `
  --include-newer --max-shards 0
```

This can run in another terminal while collection writes its own directory.
`--max-shards 0` scans all name shards. `--include-newer` expands the package
selection inside the frozen registry; it does not add packages outside it. The
current publisher query searches publications from 2019 through 2025.

Inspect `publisher-notices.jsonl` in that discovery directory. The output has
unreviewed leads with `label: null`. Repeating the same command reuses checked
query receipts. If quota or network errors interrupt it, rerun it. Once every
shard is complete, repeating the same cached search does not produce additional
leads. Use a fresh discovery directory if the selection or search mode changes.

For a new queue of checked package-cutoffs, the repository scanner can collect
dated README, changelog, release, issue, and comment evidence:

```powershell
# Set this to a real JSONL queue containing package_id, repo_group_id,
# cutoff_at, and horizon_end_at. Use new targets for each new review batch.
$targets = "$growth\review-targets-next.jsonl"
$scanId = Get-Date -Format 'yyyyMMdd-HHmmssfff'
$scanDir = "$growth\notice-scan-$scanId"
& $py -m scripts.discover_d09_repo_notices `
  --targets $targets --output-dir $scanDir `
  --use-gh --max-targets 50 --max-revisions 16 `
  --max-history-pages 6 --workers 5
```

`--max-targets` selects the first N queue entries. Rerunning with the same queue
does not advance to the next N. Make the next queue contain different targets,
or raise the limit deliberately. To resume an interrupted scan, reuse its exact
`$scanDir` and command rather than generating another identifier. Check the
reported limits before treating follow-up coverage as complete.

## Review outcomes and add a batch

For each candidate, verify package scope, source authority, original publication
and effective dates, the complete 12-month outcome window, and any resumed
maintenance. An inactive package alone is not a confirmed cessation. A negative
label needs verified maintenance and a complete follow-up review. Unknown cases
remain outside supervised training.

Write only the new approved `OutcomeReview` records to a new JSONL file. An
existing format example is
`data/d09/model-a-minimum-v1/approved-positive-mapping-v1.jsonl`; its two approvals
are already in `sample-v9`, so do not add those records again. The full-drive
runner's `--reviews` expects a different `ReviewedD09Target` format. Pass these
`OutcomeReview` files to `prepare_d09_model_a --outcomes`.

Each approved package-cutoff must have `mapping_sources_matched` in the mapping
run passed to preparation. Use `drive-full-v1` when that mapping is present. If
the mapping was verified in a targeted development run, set `$mappingRun` to
that run instead. Split approvals across preparation batches if their verified
mappings live in different runs.

After creating the first approval file, repeat this block for each new batch.
Change `$batchId` to `batch-002`, `batch-003`, and so on for subsequent batches:

```powershell
$batchId = 'batch-001'
$approved = "$growth\approved-$batchId.jsonl"
$mappingRun = "$base\drive-full-v1"
if (-not (Test-Path -LiteralPath $approved -PathType Leaf)) {
    throw 'Create the new reviewed OutcomeReview JSONL before preparing this batch.'
}

$prep = "$growth\prepared-$batchId"
$next = "$growth\sample-$batchId"

& $py -m scripts.prepare_d09_model_a `
  --run-dir $mappingRun `
  --project-cache-dir "$base\model-a-retrospective-v1" `
  --outcomes $approved --approved-only `
  --output-dir $prep --max-packages 0 `
  --repair-artifact-budget 20 --workers 4
if ($LASTEXITCODE -ne 0) { throw 'Preparation failed. Fix its findings before merging.' }

& $py -m scripts.build_model_a_sample `
  --prepared-dir $current `
  --prepared-dir $prep `
  --registry "$base\drive-full-v1\historical-registry.json" `
  --output-dir $next
if ($LASTEXITCODE -ne 0) { throw 'Assembly failed. The previous sample is still current.' }

$current = $next
Write-Host "Current dataset: $current"
Get-Content "$current\training-readiness.json"
```

Use a separate approved file for the next batch and keep each merged approval
file unchanged. If preparation needs a retry, rerun its command with the same
`$prep` and approvals. The repair budget allows at most 20 original release-file
retrievals per preparation invocation. Review the diagnostics before retrying.

Merge the previous assembled sample plus the new prepared batch only. The
previous sample already includes its earlier inputs. The assembler currently
requires one cutoff per package and repository across the entire dataset, so
new approvals must add independent repository groups. It rejects duplicate
packages or repositories instead of silently counting them again.

`training-readiness.json` reports counts and any remaining minimum deficits.
Continue adding independent reviewed rows after the deficits reach zero. The
assembler keeps all qualifying rows; it does not truncate a ready sample.

## Train a frozen dataset version

When ready to fit another model, train from the latest assembled sample:

```powershell
$runId = Get-Date -Format 'yyyyMMdd-HHmmssfff'
& $py -m scripts.train_model_a `
  --data-dir $current `
  --output-dir "$growth\runs\$runId" `
  --require-minimum-sample
```

The gate still applies to the rows left after the chronological and repository
checks. Fitting uses all eligible training rows beyond that floor. Current
datasets are case-selected retrospective samples. For real-world performance
claims, define a representative cohort and a separate frozen holdout; keep
model selection on training and validation data.

The existing frozen registry has 54,440 historical packages. These commands can
work through that cohort without stopping at the minimum. After it is exhausted,
expanding membership or dates requires a separately frozen collection version.
