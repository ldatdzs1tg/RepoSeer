# D09 team collection

Ten computers can process different whole batches of the same frozen D09 cohort.
Each member keeps a local working folder and publishes checksum-checked ZIP
checkpoints to a mounted shared Google Drive folder. This workflow is separate
from the D1/D2/C1–C7 crawler assignments. Collection does not assign outcome labels.

## Captain: initialize the shared plan

All ten members need access to the existing `RepoSeer-Data` folder. Their mounted
paths may differ. Use a new D09-specific subfolder, separate from screening data.

```powershell
$repo = 'D:\coding\HUST\DataScience\RepoSeer'
Set-Location $repo
$shared = 'G:\.shortcut-targets-by-id\1fkcK7UsBtiq7-7_4_aH3FihdjYfIku0f\RepoSeer-Data\d09-team\model-a-team-v1'
& "$repo\.venv\Scripts\python.exe" -m scripts.d09_team init `
  --source-run "$repo\data\d09\drive-full-v1" `
  --inventory-dir "$repo\data\d09\archive-inventory-2019-2025" `
  --shared-dir $shared --members 10 --stop-after-batches 100
```

Initialization preserves completed and available in-progress checkpoints, shares
verified archive excerpts, freezes the roster/batch order/inventory, and divides
unfinished work among members 1–10. The scope is the first 100 batches total,
including completed work. Rerun with identical settings to publish later saved
checkpoints without changing assignments. A different scope needs a new plan;
combined results can seed it. Packages grouped in one batch stay together.

The shared folder contains `team-plan.json`, `assignments.csv`, `README.md`,
`Start-Member.ps1`, a frozen client ZIP, and data bundles with READY/checksum
markers. The client includes project Python code, dependency metadata and the
README. It excludes `.env`, credentials, virtual environments and Git history,
so participants can use it without waiting for a Git push.

## Switch the existing collector to member 1

Stop the original collector with Ctrl+C and let it exit before the team starts.
Reuse its working folder to preserve partial downloads. Member 1 receives the
first unfinished batch at initialization.

```powershell
& "$repo\.venv\Scripts\python.exe" -m scripts.d09_team run `
  --shared-dir $shared --member 1 `
  --work-dir "$repo\data\d09\drive-full-v1" `
  --archive-workers 8 --max-downloads 200
```

One collector writes each local working folder. The old serial collector must
stop because its remaining work would overlap the distributed assignments.

## Other members

Each person needs Python 3.12+, `uv`, Google Drive for desktop, and access to the
shared checkpoint folders. Give each person a different member number. After
Drive finishes downloading the setup, run:

```powershell
$shared = 'G:\My Drive\RepoSeer-Data\d09-team\model-a-team-v1'
& "$shared\Start-Member.ps1" -Member 2
```

Replace the shared path and number for that person. The script verifies the
client ZIP and starts a private environment and local work folder under the
user's profile. It defaults to four archive workers. The Python command accepts
1–16 workers and a custom `--work-dir`. Ten people share connection/provider
capacity, so speedup depends on their networks and quotas.

## Shared checkpoints and handoff

Workers attempt to publish new archive proofs, available batch checkpoints and
heartbeats every 120 seconds, after each pass and on a normal Ctrl+C exit.
Large checkpoints can take longer to package and sync. Unchanged batch
checkpoints reuse their published bundles. A moving input is
deferred instead of publishing inconsistent data. Local work survives sync errors.
Peers import verified proof packs between passes. Different batches can still
need the same archive before another member's proof pack arrives.

Drive sync is eventually consistent. Local publication is not a cloud upload
receipt. Wait for sync to finish on both computers before handing off. Missing,
partial or checksum-mismatched bundles are treated as not yet synced. Use only
one active computer per member number. A recent heartbeat checks for accidental
reuse; it is not a distributed filesystem lock. Its stale-owner check expires
after ten minutes following an abrupt failure.

Completed archive proofs are shared automatically. Unfinished `.part` files stay
local to avoid repeatedly uploading large changing files. To transfer those
bytes too, stop the member and publish a final checkpoint:

```powershell
& "$repo\.venv\Scripts\python.exe" -m scripts.d09_team sync `
  --shared-dir $shared --member 1 `
  --work-dir "$repo\data\d09\drive-full-v1" --include-partials
```

Another computer can then resume that member number in a new local working
folder. The client restores saved batch checkpoints and any published partial
bytes. It keeps existing local batch work in preference to a shared restore.

For a background member, request a graceful stop at its next bounded pass:

```powershell
& "$repo\.venv\Scripts\python.exe" -m scripts.d09_team stop `
  --work-dir "$repo\data\d09\drive-full-v1"
```

It publishes the saved checkpoint before exiting. Restarting the member clears
the old stop request and resumes the assignment.

## View progress and assemble data for review

```powershell
& "$repo\.venv\Scripts\python.exe" -m scripts.d09_team status --shared-dir $shared

$combined = "$repo\data\d09\team-combined-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
& "$repo\.venv\Scripts\python.exe" -m scripts.d09_team assemble `
  --shared-dir $shared --output-dir $combined

& "$repo\.venv\Scripts\python.exe" -m scripts.audit_d09_readiness `
  --run-dir $combined --inventory-dir "$combined\team-inventory" `
  --output-dir "$combined-review"
```

Status reports combined completed/remaining batches, mapping counts, shared
proofs and member heartbeats as synced to that computer. Assembly restores the
latest completed checkpoints into a new directory, even before everyone finishes.
Use another new directory for a later snapshot. Original batch IDs and the
`prepare_d09_model_a --run-dir` layout are preserved. Outcome approvals still
enter preparation through `--outcomes --approved-only`.

## Update the frozen client

When collector fixes are needed, stop active team members first. The captain can
publish the updated local code without copying existing dataset bundles again:

```powershell
& "$repo\.venv\Scripts\python.exe" -m scripts.d09_team update-client --shared-dir $shared
```

The update records its previous verified plan and permits reuse only when the
entire frozen cohort and assignments match. Launchers use a separate client
directory per runtime version, so the next start picks up the published code.
