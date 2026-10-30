"""Mounted Drive-only contracts; shared folders and collectors are entirely offline."""

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from tests.coordination import test_coordination as coordination_tests
from tools.coordination import _common as c
from tools.coordination import dispatcher as d
from tools.coordination import worker as w
from typer.testing import CliRunner

from reposeer.cli.app import app
from reposeer.exceptions import HTTPClientError
from reposeer.pipelines.package_discovery import PackageDiscoveryOptions, PackageDiscoveryPipeline
from reposeer.storage import screening_mapping as sm

environment = coordination_tests.environment
tmp_path = coordination_tests.tmp_path


@pytest.fixture(autouse=True)
def no_legacy_services(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Drive-only must not instantiate Registry/rclone or an unmocked PyPI client")

    monkeypatch.delenv("REPOSEER_REGISTRY_DSN", raising=False)
    monkeypatch.delenv("REPOSEER_DRIVE_REMOTE", raising=False)
    monkeypatch.setattr(subprocess, "run", forbidden)
    for module, name in (
        (c, "Registry"),
        (d, "Registry"),
        (c, "Drive"),
        (d, "Drive"),
        (w, "Drive"),
        (d, "PyPIClient"),
    ):
        monkeypatch.setattr(module, name, forbidden)


class PyPI:
    def __init__(self, repositories=None):
        self.repositories = repositories or {}
        self.calls = []

    def get_package_info(self, name):
        self.calls.append(name)
        if name == "missing":
            raise HTTPClientError("private-provider-body", status_code=404)
        if name == "network":
            raise HTTPClientError("private-provider-body", status_code=429)
        urls = {"Source": "https://github.com/" + self.repositories.get(name, "pallets/flask")}
        if name == "unresolved":
            urls = {"Home": "https://github.com/pallets/flask"}
        if name == "ambiguous":
            urls["Code"] = "https://github.com/other/repository"
        return {"info": {"name": name, "project_urls": urls}}


def discovery(tmp_path, names):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / (uuid4().hex + ".txt")
    source.write_text("\n".join(names), encoding="utf-8")
    result = PackageDiscoveryPipeline().run(
        PackageDiscoveryOptions(
            source="file",
            count=len(names),
            data_dir=tmp_path / "d1",
            input_file=source,
        )
    )
    root = tmp_path / "ổ chung" / "RepoSeer-Data"
    run = root / "discovery/submissions" / result.sampling_run_id
    for name in ("manifest.json", "package_candidates.csv"):
        c.copy_verified(result.manifest_path.parent / name, run / name)
    d.bootstrap_mapping(root)
    return root, run, result.sampling_run_id


def test_screen_drive_mapping_allocation_publication_and_replay(tmp_path, monkeypatch):
    root, run, run_id = discovery(
        tmp_path, ["a", "b", "c", "d", "unresolved", "ambiguous", "network"]
    )
    client = PyPI({"a": "Owner/X", "b": "owner/x", "c": "owner/y", "d": "owner/z"})
    # CLI dry-run validates input before constructing any legacy Registry/client.
    monkeypatch.setattr(
        sys, "argv", ["dispatcher.py", "screen-drive", "--run-dir", str(run), "--dry-run"]
    )
    assert d.main()["status"] == "planned"
    assert not (root / "assignments").exists()
    result = d.screen_drive(run, data_root=tmp_path / "s", client=client)
    assert {key: result[key] for key in ("candidate", "unresolved", "ambiguous", "failed")} == dict(
        candidate=4,
        unresolved=1,
        ambiguous=1,
        failed=1,
    )
    marker, mapping, assigned = c.verify_drive_screening(root, run_id)
    assert len(client.calls) == len(mapping) == 7
    assert [row["package_id"] for row in assigned["C1"]] == ["pypi:a", "pypi:b"]
    assert assigned["C2"][0]["package_id"] == "pypi:c"
    assert assigned["C3"][0]["package_id"] == "pypi:d"
    assert all(not assigned[worker] for worker in ("C4", "C5", "C6", "C7"))
    assert marker["assigned_package_count"] == sum(map(len, assigned.values())) == 4
    assert c.allocate_drive_assignments(list(reversed(mapping))) == assigned
    evidence = {
        row["package_id"]: row
        for row in map(
            json.loads,
            (root / f"screening/runs/{run_id}/pypi_evidence.jsonl")
            .read_text(encoding="utf-8")
            .splitlines(),
        )
    }
    for row in mapping:
        if row["screening_status"] != "failed":
            assert row["evidence_sha256"] == c.digest(c.canonical(evidence[row["package_id"]]))
    before = c.files(root)
    alias_mtime = (root / "screening/mapping.csv").stat().st_mtime_ns
    assert d.screen_drive(run, data_root=tmp_path / "s", client=client)["reused"]
    assert len(client.calls) == 7 and c.files(root) == before
    assert (root / "screening/mapping.csv").stat().st_mtime_ns == alias_mtime
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker.py",
            "crawl-drive",
            "--assignment",
            str(root / f"assignments/C1/{run_id}.csv"),
            "--dry-run",
        ],
    )
    assert w.main()["parts"] == 1
    # Replaying an older run preserves the full cumulative master and its metadata.
    _, newer_run, newer_id = discovery(tmp_path, ["new"])
    d.screen_drive(newer_run, data_root=tmp_path / "s", client=PyPI())
    master_before = c.files(root / "screening")
    d.screen_drive(run, data_root=tmp_path / "s", client=client)
    assert c.files(root / "screening") == master_before
    assert sm.read_cumulative_mapping(root)[0]["row_count"] == 8
    assert (root / f"screening/runs/{newer_id}/READY.json").exists()
    c.atomic_bytes(root / f"screening/runs/{run_id}/unexpected.txt", b"undeclared")
    with pytest.raises(c.CoordinationError, match="unexpected_screening_files"):
        w.crawl_drive(root / f"assignments/C1/{run_id}.csv", dry_run=True)


def test_screen_drive_rejects_changed_discovery_and_never_publishes_early(tmp_path, monkeypatch):
    root, run, run_id = discovery(tmp_path, ["flask"])
    path = run / "package_candidates.csv"
    original = path.read_bytes()
    path.write_bytes(original + b"changed")
    with pytest.raises(c.CoordinationError, match="sampling_csv_checksum_mismatch"):
        d.screen_drive(run, data_root=tmp_path / "s", client=PyPI())
    assert not (root / "assignments").exists()
    path.write_bytes(original)
    client = PyPI()
    copy = d.copy_verified

    def interrupted(source, target, **kwargs):
        if target.name == f"{run_id}.csv" and target.parent.name == "C7":
            raise OSError("simulated incomplete publication")
        return copy(source, target, **kwargs)

    monkeypatch.setattr(d, "copy_verified", interrupted)
    with pytest.raises(OSError):
        d.screen_drive(run, data_root=tmp_path / "s", client=client)
    assert not (root / f"screening/runs/{run_id}/READY.json").exists()
    assert sm.read_cumulative_mapping(root)[0]["row_count"] == 0
    with pytest.raises(OSError):
        w.crawl_drive(root / f"assignments/C1/{run_id}.csv", data_root=tmp_path / "w")
    monkeypatch.setattr(d, "copy_verified", copy)
    assert d.screen_drive(run, data_root=tmp_path / "s", client=client)["status"] == "completed"
    assert client.calls == ["flask"]


def test_crawl_drive_validates_all_assignments_and_plans_bounded_parts(tmp_path):
    root, run, run_id = discovery(tmp_path, [f"pkg-{index:03d}" for index in range(101)])
    d.screen_drive(run, data_root=tmp_path / "s", client=PyPI())
    assignment = root / f"assignments/C1/{run_id}.csv"
    planned = w.crawl_drive(assignment, data_root=tmp_path / "w", dry_run=True)
    assert (planned["packages"], planned["repositories"], planned["parts"]) == (101, 1, 2)
    assert not (tmp_path / "w").exists()
    with pytest.raises(c.CoordinationError, match="workspace_must_be_local"):
        w.crawl_drive(assignment, data_root=root / "work", dry_run=True)
    assert w.crawl_drive(root / f"assignments/C2/{run_id}.csv", dry_run=True)["parts"] == 0
    original = assignment.read_bytes()
    assignment.write_bytes(original + b"tampered")
    with pytest.raises(c.CoordinationError, match="drive_assignment_checksum_mismatch"):
        w.crawl_drive(assignment, dry_run=True)
    assignment.write_bytes(original)
    _, _, assigned = c.verify_drive_screening(root, run_id)
    other = root / f"assignments/C2/{run_id}.csv"
    d.write_drive_table(
        other.parent / "duplicate.csv",
        c.ASSIGNMENT_FIELDS,
        [dict(assigned["C1"][0], worker_id="C2")],
    )
    other.write_bytes((other.parent / "duplicate.csv").read_bytes())
    marker_path = root / f"screening/runs/{run_id}/READY.json"
    marker = c.read_json(marker_path)
    marker["assignments_sha256"]["C2"] = c.fingerprint(other)[0]
    c.write_json(marker_path, marker, immutable=False)
    with pytest.raises(c.CoordinationError, match="drive_assignment_allocation_mismatch"):
        w.crawl_drive(assignment, dry_run=True)


@pytest.mark.parametrize("mode", ["resume", "mapping_mismatch", "interrupted"])
def test_crawl_drive_uses_real_evidence_and_preserves_retry_state(
    tmp_path, environment, monkeypatch, mode
):
    root, run, run_id = discovery(tmp_path, ["flask"])
    client = PyPI({"flask": "other/repository"} if mode == "mapping_mismatch" else None)
    d.screen_drive(run, data_root=tmp_path / "s", client=client)
    marker_path = root / f"screening/runs/{run_id}/READY.json"
    legacy_marker = c.read_json(marker_path)
    legacy_marker.pop("collection_policy")
    c.write_json(marker_path, legacy_marker, immutable=False)
    assignment = root / f"assignments/C1/{run_id}.csv"
    pipeline, paths, _, options, _ = environment
    if mode != "interrupted":
        options["fail_provider"] = True
        saved = pipeline.run(["pypi:flask"], **c.collection_options("initial"))
        assert saved.status == "partial"
    calls = []
    monkeypatch.setenv("REPOSEER_REGISTRY_DSN", "coordinator-only-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "worker-token-secret")

    def runner(command, *, stdout, env, **kwargs):
        assert "REPOSEER_REGISTRY_DSN" not in env
        assert command[:5] == ["uv", "run", "--frozen", "reposeer", "collect-dataset"]
        assert command[command.index("--max-pages") + 1] == "1"
        assert command[command.index("--max-records") + 1] == "30"
        calls.append(command)
        destination = Path(command[command.index("--data-dir") + 1])
        if mode == "interrupted":
            c.write_json(
                destination / f"metadata/collection_runs/integration_{uuid4()}.json",
                dict(status="partial", finished_at=None),
            )
        else:
            shutil.copytree(paths.base_dir, destination, dirs_exist_ok=True)
        stdout.write(b"worker-token-secret\n")
        return subprocess.CompletedProcess(command, 1)

    copy = w.copy_verified

    def interrupted_upload(source, target, **kwargs):
        if target.name == "READY.json":
            raise OSError("simulated upload interruption")
        return copy(source, target, **kwargs)

    if mode == "resume":
        monkeypatch.setattr(w, "copy_verified", interrupted_upload)
    first = w.crawl_drive(assignment, data_root=tmp_path / "w", runner=runner)
    assert first["failed"] == 1 and len(calls) == 1
    assert not list((root / "submissions").rglob("READY.json"))
    if mode != "resume":
        assert not list((tmp_path / "w").rglob("READY.json"))
        second = w.crawl_drive(assignment, data_root=tmp_path / "w", runner=runner)
        assert second["failed"] == 1 and len(calls) == 1
        return
    monkeypatch.setattr(w, "copy_verified", copy)
    second = w.crawl_drive(assignment, data_root=tmp_path / "w", runner=runner)
    assert second["partial"] == 1 and second["failed"] == 0 and len(calls) == 1, second
    destination = next((root / "submissions").rglob("READY.json")).parent
    marker = c.read_json(destination / "READY.json")
    assert marker["collection_status"] == "partial"
    assert marker["integration_run_id"] == saved.integration_run_id
    assert len(marker["repository_id"]) == 64
    assert marker["repository_id"] == c.digest(b"pallets/flask")
    assert (destination / "raw/github/repositories.jsonl").is_file()
    assert list((destination / "processed").rglob("*.parquet"))
    assert not list(destination.rglob("*.log"))
    c.atomic_bytes(destination / "raw/desktop.ini", b"OS metadata")
    assert (
        w.crawl_drive(assignment, data_root=tmp_path / "fresh", runner=runner)["skipped_existing"]
        == 1
    )
    assert not (tmp_path / "fresh").exists() and len(calls) == 1
    unexpected = destination / "unexpected.txt"
    c.atomic_bytes(unexpected, b"undeclared")
    assert w.crawl_drive(assignment, data_root=tmp_path / "fresh", runner=runner)["failed"] == 1
    unexpected.unlink()
    raw = destination / "raw/github/repositories.jsonl"
    original = raw.read_bytes()
    raw.write_bytes(original + b"corrupt")
    assert w.crawl_drive(assignment, data_root=tmp_path / "fresh", runner=runner)["failed"] == 1
    assert len(calls) == 1 and raw.read_bytes().endswith(b"corrupt")
    raw.write_bytes(original)
    raw.unlink()
    assert w.crawl_drive(assignment, data_root=tmp_path / "fresh", runner=runner)["failed"] == 1
    assert len(calls) == 1


@pytest.mark.parametrize("total", [50, 51])
def test_screening_progress_non_tty_snapshots_are_bounded(total, capsys, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    with d._ScreeningProgress(total) as progress:
        for index in range(total):
            progress.advance({"screening_status": "candidate"}, is_cached=index < 25)
            assert sum(progress.counts.values()) == progress.processed == index + 1
    captured = capsys.readouterr()
    lines = captured.err.splitlines()
    assert captured.out == "" and "\x1b" not in captured.err
    assert len(lines) == (total + 24) // 25
    assert [line.split()[1] for line in lines] == [
        f"{count}/{total}" for count in sorted({25, 50, total})
    ]
    assert f"{total}/{total} (100.0%)" in lines[-1]
    assert f"Mapped: {total}" in lines[-1]
    assert "Unmapped: 0 | Cached: 25 | Remaining: 0" in lines[-1]
    assert "Elapsed:" in lines[-1] and "ETA: 0:00:00" in lines[-1]


def test_screening_progress_live_counters_and_cleanup(capsys, monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    console = d.Console
    monkeypatch.setattr(d, "Console", lambda **kwargs: console(width=42, **kwargs))
    with d._ScreeningProgress(4) as progress:
        for index, status in enumerate(("candidate", "unresolved", "ambiguous", "failed"), 1):
            progress.advance({"screening_status": status}, is_cached=index <= 2)
            assert sum(progress.counts.values()) == progress.processed == index
            assert progress.counts[status] == 1
    assert progress.cached == 2
    assert progress.bar.tasks[0].percentage == 100.0
    assert not progress.live.is_started
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "D2 Screening PyPI Packages" in captured.err
    assert "4/4 (100.0%)" in captured.err
    assert "Unmapped: 3" in captured.err
    with pytest.raises(KeyboardInterrupt), d._ScreeningProgress(4) as interrupted:
        interrupted.advance({"screening_status": "candidate"}, is_cached=True)
        raise KeyboardInterrupt
    assert interrupted.processed == 1 and not interrupted.live.is_started
    assert "100.0%" not in capsys.readouterr().err


def test_screen_drive_progress_counts_persisted_rows_and_resumes(tmp_path, capsys, monkeypatch):
    root, run, run_id = discovery(
        tmp_path, ["mapped", "unresolved", "ambiguous", "network", "missing"]
    )
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    workspace = tmp_path / "s"
    client = PyPI()
    instances = []
    original_progress = d._ScreeningProgress
    original_write = d.write_json
    writes = 0

    class ObservedProgress(original_progress):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.closed = False
            instances.append(self)

        def advance(self, row, *, is_cached):
            checkpoint = (
                workspace
                / run_id
                / "results"
                / (c.digest(row["package_id"].encode("utf-8")) + ".json")
            )
            saved = c.read_json(checkpoint)
            assert saved["row"] == row  # Report only a successfully persisted result.
            assert set(saved) == {"source_hashes", "row", "evidence"}
            super().advance(row, is_cached=is_cached)
            assert sum(self.counts.values()) == self.processed

        def __exit__(self, *args):
            super().__exit__(*args)
            self.closed = True

    def interrupted_write(path, value):
        nonlocal writes
        if path.parent.name == "results":
            writes += 1
            if writes == 3:
                raise OSError("simulated checkpoint write interruption")
        return original_write(path, value)

    monkeypatch.setattr(d, "_ScreeningProgress", ObservedProgress)
    monkeypatch.setattr(d, "write_json", interrupted_write)
    with pytest.raises(OSError, match="checkpoint write interruption"):
        d.screen_drive(run, data_root=workspace, client=client)
    assert instances[0].processed == 2 and instances[0].closed
    assert "100.0%" not in capsys.readouterr().err
    assert not list(root.rglob("READY.json"))
    assert not list(workspace.rglob("READY.json"))
    checkpoints = {path: path.read_bytes() for path in workspace.rglob("results/*.json")}
    assert len(checkpoints) == 2
    monkeypatch.setattr(d, "write_json", original_write)
    result = d.screen_drive(run, data_root=workspace, client=client)
    assert result["status"] == "completed"
    assert instances[1].closed and instances[1].processed == instances[1].total == 5
    assert instances[1].counts == dict(candidate=1, unresolved=1, ambiguous=1, failed=2)
    assert instances[1].cached == 2
    assert len(client.calls) == 6
    assert all(client.calls.count(name) == 1 for name in client.calls[:2])
    assert all(path.read_bytes() == data for path, data in checkpoints.items())
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "5/5 (100.0%)" in captured.err
    assert "Mapped: 1 | Unresolved: 1 | Ambiguous: 1 | Failed: 2" in captured.err
    assert "Unmapped: 4 | Cached: 2 | Remaining: 0" in captured.err
    _, mapping, _ = c.verify_drive_screening(root, run_id)
    missing = next(row for row in mapping if row["package_name"] == "missing")
    assert (missing["screening_status"], missing["reason_code"]) == (
        "failed",
        "pypi_request_failed",
    )
    assert d.screen_drive(run, data_root=workspace, client=client)["reused"]
    assert len(instances) == 2 and len(client.calls) == 6
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("flag", [None, "--dry-run", "--no-progress"])
def test_screen_drive_cli_progress_modes_preserve_json(flag, tmp_path, capsys, monkeypatch):
    root, run, _ = discovery(tmp_path, ["mapped"])
    workspace = tmp_path / "s"
    before = c.files(root)
    client = PyPI()
    monkeypatch.setattr(d, "PyPIClient", lambda **kwargs: client)
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    progress_calls = []
    original_progress = d._ScreeningProgress

    def observed_progress(*args, **kwargs):
        progress_calls.append(kwargs["enabled"])
        return original_progress(*args, **kwargs)

    monkeypatch.setattr(d, "_ScreeningProgress", observed_progress)
    argv = ["dispatcher.py", "screen-drive", "--run-dir", str(run), "--data-root", str(workspace)]
    monkeypatch.setattr(sys, "argv", argv + ([flag] if flag else []))
    c.cli_guard(d.main)
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    result = json.loads(captured.out)
    assert result["package_count"] == 1
    assert "\x1b" not in captured.out + captured.err
    if flag == "--dry-run":
        assert result["status"] == "planned" and result["dry_run"]
        assert not client.calls and not progress_calls
        assert not workspace.exists() and c.files(root) == before
    else:
        assert result["status"] == "completed" and client.calls == ["mapped"]
        assert set(result) == {
            "status",
            "sampling_run_id",
            "reused",
            "package_count",
            "input_package_count",
            "skipped_existing_count",
            "skipped_existing_examples",
            "candidate",
            "unresolved",
            "ambiguous",
            "failed",
            "assignments",
            "mapping",
            "master",
        }
        assert progress_calls == [flag != "--no-progress"]
    if flag is None:
        assert "[D2] 1/1 (100.0%)" in captured.err
    else:
        assert captured.err == ""


def historical_drive(tmp_path, batches):
    root = tmp_path / "historical" / "RepoSeer-Data"
    run_ids = []
    for index, names in enumerate(batches):
        source, run, run_id = discovery(tmp_path / f"source-{index}", names)
        d.screen_drive(run, data_root=tmp_path / f"stage-{index}", client=PyPI())
        for name in ("discovery", "assignments", "screening/runs"):
            shutil.copytree(source / name, root / name, dirs_exist_ok=True)
        run_ids.append(run_id)
    latest = max(
        run_ids,
        key=lambda run: (
            c.read_json(root / f"screening/runs/{run}/READY.json")["generated_at_utc"],
            run,
        ),
    )
    c.copy_verified(root / f"screening/runs/{latest}/mapping.csv", root / "screening/mapping.csv")
    return root, run_ids


@pytest.mark.parametrize(
    "batches",
    [[["old", "network", "unresolved"]], [["old", "network"], ["old", "new", "unresolved"]]],
)
def test_bootstrap_verified_history_and_immutable_first_seen(tmp_path, monkeypatch, batches):
    root, run_ids = historical_drive(tmp_path, batches)
    before = c.files(root)
    plan = d.bootstrap_mapping(root, dry_run=True)
    assert plan["status"] == "planned" and c.files(root) == before
    result = d.bootstrap_mapping(root)
    meta, rows = sm.read_cumulative_mapping(root)
    expected = {}
    for run_id in sorted(
        run_ids,
        key=lambda run: (
            c.read_json(root / f"screening/runs/{run}/READY.json")["generated_at_utc"],
            run,
        ),
    ):
        for row in c.verify_drive_screening(root, run_id)[1]:
            expected.setdefault(row["package_id"], row)
    assert rows == expected
    assert list(rows) == sorted(expected)
    assert result["total_rows_read"] == sum(map(len, batches))
    assert result["total_unique_packages"] == meta["row_count"] == len(expected)
    assert result["historical_duplicates"] == sum(map(len, batches)) - len(expected)
    published = c.files(root)
    for name, checksum in before.items():
        if name != "screening/mapping.csv":
            assert published[name] == checksum
    after = c.files(root)
    monkeypatch.setattr(
        d, "_verified_history", lambda *args: pytest.fail("Initialized master rescanned history")
    )
    assert d.bootstrap_mapping(root)["status"] == "already_initialized"
    for run_id in run_ids:
        counts = d.merge_cumulative_mapping(root, run_id)
        assert counts["new_packages_added"] == 0
        if len(run_ids) == 2 and run_id == run_ids[1]:
            assert counts["historical_duplicates"] == 1
    assert c.files(root) == after


def test_bootstrap_rejects_unexpected_alias_and_corrupt_initialized_master(tmp_path):
    root, _ = historical_drive(tmp_path, [["old"]])
    alias = root / "screening/mapping.csv"
    expected = alias.read_bytes()
    alias.write_bytes(b"unexpected")
    before = c.files(root)
    with pytest.raises(c.CoordinationError, match="legacy_mapping_alias_mismatch"):
        d.bootstrap_mapping(root)
    assert c.files(root) == before
    alias.write_bytes(expected)
    d.bootstrap_mapping(root)
    alias.unlink()
    with pytest.raises(ValueError, match="master_mapping_missing"):
        d.bootstrap_mapping(root)
    assert not alias.exists()


def test_fresh_bootstrap_cli_and_missing_master_fail_closed(tmp_path, monkeypatch, capsys):
    root = tmp_path / "fresh"
    argv = ["dispatcher.py", "bootstrap-mapping", "--drive-root", str(root)]
    monkeypatch.setattr(sys, "argv", argv + ["--dry-run"])
    c.cli_guard(d.main)
    assert json.loads(capsys.readouterr().out)["total_unique_packages"] == 0
    assert not root.exists()
    monkeypatch.setattr(sys, "argv", argv)
    c.cli_guard(d.main)
    assert json.loads(capsys.readouterr().out)["status"] == "initialized"
    meta, rows = sm.read_cumulative_mapping(root)
    assert rows == {} and meta["last_merged_run_id"] is None
    source, run, _ = discovery(tmp_path / "new-run", ["new"])
    (source / "screening/mapping.meta.json").unlink()
    client = PyPI()
    with pytest.raises(c.CoordinationError, match="master_not_initialized_run_bootstrap_mapping"):
        d.screen_drive(run, data_root=tmp_path / "stage", client=client)
    assert not client.calls and not (source / "assignments").exists()


def test_d2_skips_overlap_and_retains_same_run_conflicts(tmp_path, monkeypatch):
    root, run, run_id = discovery(tmp_path, ["old"])
    d.screen_drive(run, data_root=tmp_path / "s", client=PyPI())
    original_row = sm.read_cumulative_mapping(root)[1]["pypi:old"]
    _, mixed, mixed_id = discovery(tmp_path, ["old", "new", "other"])
    source_ids = [row["package_id"] for row in d.discovery_bundle(mixed)[1]]
    expected = [key for key in source_ids if key != "pypi:old"]
    inputs = c.files(mixed)
    before = c.files(root)
    client = PyPI()
    monkeypatch.setattr(
        sys, "argv", ["dispatcher.py", "screen-drive", "--run-dir", str(mixed), "--dry-run"]
    )
    plan = d.main()
    assert plan == dict(
        status="planned",
        dry_run=True,
        sampling_run_id=mixed_id,
        package_count=3,
        to_screen=2,
        skipped_existing_count=1,
        skipped_existing_examples=["pypi:old"],
    )
    assert c.files(root) == before and not client.calls
    totals = []
    original_progress = d._ScreeningProgress

    def progress(total, **kwargs):
        totals.append(total)
        return original_progress(total, **kwargs)

    monkeypatch.setattr(d, "_ScreeningProgress", progress)
    result = d.screen_drive(mixed, data_root=tmp_path / "s", client=client)
    assert client.calls == [key[5:] for key in expected] and totals == [2]
    assert result["package_count"] == 2 and result["input_package_count"] == 3
    assert result["skipped_existing_count"] == 1
    assert result["skipped_existing_examples"] == ["pypi:old"]
    marker, mapping, assigned = c.verify_drive_screening(root, mixed_id)
    assert marker["skipped_existing_package_ids"] == ["pypi:old"]
    assert [row["package_id"] for row in mapping] == expected
    assert {row["package_id"] for rows in assigned.values() for row in rows} == set(expected)
    evidence = root / f"screening/runs/{mixed_id}/pypi_evidence.jsonl"
    assert {
        json.loads(line)["package_id"] for line in evidence.read_text(encoding="utf-8").splitlines()
    } == set(expected)
    checkpoints = tmp_path / "s" / mixed_id / "results"
    assert {path.stem for path in checkpoints.glob("*.json")} == {
        c.digest(key.encode()) for key in expected
    }
    assert c.files(mixed) == inputs
    assert sm.read_cumulative_mapping(root)[1]["pypi:old"] == original_row
    before = c.files(root)
    reused = d.screen_drive(mixed, data_root=tmp_path / "s", client=client)
    assert reused["reused"] and reused["skipped_existing_count"] == 1
    assert c.files(root) == before and len(client.calls) == 2
    meta, rows = sm.read_cumulative_mapping(root)
    rows["pypi:old"]["reason_code"] = "changed"
    stage = tmp_path / "changed"
    stage.mkdir()
    d._stage_master(stage, rows, run_id)
    c.copy_verified(stage / "mapping.csv", root / "screening/mapping.csv", replace=True)
    c.copy_verified(stage / "mapping.meta.json", root / "screening/mapping.meta.json", replace=True)
    before = c.files(root)
    with pytest.raises(c.CoordinationError, match="master_same_run_conflict"):
        d.screen_drive(run, data_root=tmp_path / "s", client=client)
    assert len(client.calls) == 2 and c.files(root) == before


def test_d2_all_duplicate_run_publishes_empty_results_without_clients(tmp_path, monkeypatch):
    names = ["old", "network", "unresolved", "ambiguous", "other", "extra"]
    root, first, _ = discovery(tmp_path, names)
    d.screen_drive(first, data_root=tmp_path / "s", client=PyPI())
    assert {row["screening_status"] for row in sm.read_cumulative_mapping(root)[1].values()} == {
        "candidate",
        "failed",
        "unresolved",
        "ambiguous",
    }
    _, run, run_id = discovery(tmp_path, names)
    skipped = [row["package_id"] for row in d.discovery_bundle(run)[1]]
    master_paths = [root / "screening" / name for name in ("mapping.csv", "mapping.meta.json")]
    master_before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in master_paths]

    def forbidden(*args, **kwargs):
        pytest.fail("All-duplicate runs must not initialize clients or progress")

    monkeypatch.setattr(d, "HTTPClient", forbidden)
    monkeypatch.setattr(d, "_ScreeningProgress", forbidden)
    before = c.files(root)
    plan = d.screen_drive(run, data_root=tmp_path / "s", dry_run=True)
    assert plan["to_screen"] == 0 and plan["package_count"] == len(names)
    assert plan["skipped_existing_count"] == len(names)
    assert plan["skipped_existing_examples"] == skipped[:5]
    assert c.files(root) == before
    result = d.screen_drive(run, data_root=tmp_path / "s")
    assert result["package_count"] == 0 and result["input_package_count"] == len(names)
    assert result["skipped_existing_count"] == len(names)
    assert result["skipped_existing_examples"] == skipped[:5]
    marker, mapping, assigned = c.verify_drive_screening(root, run_id)
    assert mapping == [] and all(rows == [] for rows in assigned.values())
    assert marker["skipped_existing_package_ids"] == skipped
    assert marker["schema_version"] == 1
    output = root / f"screening/runs/{run_id}"
    assert (output / "mapping.csv").read_bytes() == (",".join(c.MAPPING_FIELDS) + "\n").encode()
    assert (output / "pypi_evidence.jsonl").read_bytes() == b""
    for worker in c.DRIVE_WORKERS:
        assert (root / f"assignments/{worker}/{run_id}.csv").read_bytes() == (
            ",".join(c.ASSIGNMENT_FIELDS) + "\n"
        ).encode()
    assert not (tmp_path / "s" / run_id / "results").exists()
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in master_paths] == master_before
    before = c.files(root)
    assert d.screen_drive(run, data_root=tmp_path / "s")["reused"]
    assert c.files(root) == before
    for invalid in (
        None,
        "pypi:old",
        [skipped[0]] * 2,
        ["pypi:unknown"],
        list(reversed(skipped)),
        ["pypi:OLD"],
        [],
    ):
        with pytest.raises(c.CoordinationError):
            c.verify_drive_screening(
                root, run_id, marker=dict(marker, skipped_existing_package_ids=invalid)
            )


def test_d2_legacy_ready_and_new_assignments_remain_compatible(tmp_path):
    root, first, first_id = discovery(tmp_path, ["old"])
    d.screen_drive(first, data_root=tmp_path / "s", client=PyPI())
    ready = root / f"screening/runs/{first_id}/READY.json"
    legacy = c.read_json(ready)
    legacy.pop("skipped_existing_package_ids")
    c.write_json(ready, legacy, immutable=False)  # Simulate a published pre-skip run.
    assert len(c.verify_drive_screening(root, first_id)[1]) == 1
    client = PyPI()
    before = c.files(root)
    reused = d.screen_drive(first, data_root=tmp_path / "s", client=client)
    assert reused["reused"] and reused["skipped_existing_count"] == 0
    assert reused["input_package_count"] == 1
    assert not client.calls and c.files(root) == before
    _, run, run_id = discovery(tmp_path, ["old", "fresh"])
    d.screen_drive(run, data_root=tmp_path / "s", client=client)
    assert client.calls == ["fresh"]
    result = w.crawl_drive(
        root / f"assignments/C1/{run_id}.csv", data_root=tmp_path / "w", dry_run=True
    )
    assert result["packages"] == result["parts"] == 1
    assert not (tmp_path / "w").exists()


@pytest.mark.parametrize("phase", ["mapping.csv", "mapping.meta.json", "verification"])
def test_master_publication_recovery_blocks_d1_and_reuses_published_run(
    tmp_path, monkeypatch, phase
):
    root, first, first_id = discovery(tmp_path, ["old"])
    d.screen_drive(first, data_root=tmp_path / "s", client=PyPI())
    _, run, run_id = discovery(tmp_path, ["new"])
    client = PyPI()
    copy = d.copy_verified
    read = d.read_cumulative_mapping

    def interrupted_copy(source, target, **kwargs):
        if target.parent.name == "screening" and target.name == phase:
            raise OSError("interrupted master publication")
        return copy(source, target, **kwargs)

    def interrupted_verification(root, **kwargs):
        if kwargs.get("allow_pending") and phase == "verification":
            raise OSError("interrupted master verification")
        return read(root, **kwargs)

    monkeypatch.setattr(d, "copy_verified", interrupted_copy)
    monkeypatch.setattr(d, "read_cumulative_mapping", interrupted_verification)
    with pytest.raises(OSError, match="interrupted master"):
        d.screen_drive(run, data_root=tmp_path / "s", client=client)
    assert (root / f"screening/runs/{run_id}/READY.json").exists()
    assert (root / "screening/mapping.pending.json").exists()
    before = c.files(root)
    with pytest.raises(sm.ScreeningMappingError, match="master_update_pending"):
        sm.read_cumulative_mapping(root)
    source = tmp_path / "names.txt"
    source.write_text("next", encoding="utf-8")
    result = CliRunner().invoke(
        app,
        [
            "discover-packages",
            "--source",
            "file",
            "--input-file",
            str(source),
            "--count",
            "1",
            "--drive-root",
            str(root),
            "--data-dir",
            str(tmp_path / "d1-next"),
        ],
    )
    assert result.exit_code == 1 and "master_update_pending" in result.output
    assert not (tmp_path / "d1-next").exists() and c.files(root) == before
    monkeypatch.setattr(d, "copy_verified", copy)
    monkeypatch.setattr(d, "read_cumulative_mapping", read)
    result = d.screen_drive(run, data_root=tmp_path / "s", client=client)
    assert result["reused"] and client.calls == ["new"]
    assert set(sm.read_cumulative_mapping(root)[1]) == {"pypi:old", "pypi:new"}
    assert not (root / "screening/mapping.pending.json").exists()
    published = c.files(root)
    for name, checksum in before.items():
        if "/runs/" in name or name.startswith("assignments/"):
            assert published[name] == checksum
    assert c.verify_drive_screening(root, first_id)[1][0]["package_id"] == "pypi:old"
    after = c.files(root)
    assert (
        d.screen_drive(run, data_root=tmp_path / "s", client=client)["master"]["new_packages_added"]
        == 0
    )
    assert c.files(root) == after and client.calls == ["new"]


@pytest.mark.parametrize("damage", ["master", "source"])
def test_recovery_retains_unverifiable_journal_and_files(tmp_path, monkeypatch, damage):
    root, run, run_id = discovery(tmp_path, ["new"])
    copy = d.copy_verified

    def interrupted(source, target, **kwargs):
        if target.parent.name == "screening" and target.name == "mapping.csv":
            raise OSError("interrupted master")
        return copy(source, target, **kwargs)

    monkeypatch.setattr(d, "copy_verified", interrupted)
    client = PyPI()
    with pytest.raises(OSError):
        d.screen_drive(run, data_root=tmp_path / "s", client=client)
    path = root / (
        "screening/mapping.csv" if damage == "master" else f"screening/runs/{run_id}/mapping.csv"
    )
    path.write_bytes(b"conflicting bytes")
    before = c.files(root)
    monkeypatch.setattr(d, "copy_verified", copy)
    with pytest.raises(
        c.CoordinationError,
        match="master_recovery_state_conflict|drive_screening_checksum_mismatch",
    ):
        d.screen_drive(run, data_root=tmp_path / "s", client=client)
    assert c.files(root) == before and client.calls == ["new"]


def test_d1_master_and_explicit_exclusions_produce_disjoint_successive_samples(tmp_path):
    root, run, _ = discovery(tmp_path, ["old", "network", "unresolved", "ambiguous"])
    d.screen_drive(run, data_root=tmp_path / "s", client=PyPI())
    source, extra = tmp_path / "names.txt", tmp_path / "exclude.txt"
    source.write_text(
        "old\nnetwork\nunresolved\nambiguous\n" + "\n".join(f"new-{i}" for i in range(8)),
        encoding="utf-8",
    )
    extra.write_text("NEW_0\npypi:New.0\nunresolved\n", encoding="utf-8")
    original_extra = extra.read_bytes()
    runner = CliRunner()
    all_selected = set()
    for index in range(2):
        meta, rows = sm.read_cumulative_mapping(root)
        before = c.files(root)
        output = tmp_path / f"sample-{index}"
        result = runner.invoke(
            app,
            [
                "discover-packages",
                "--source",
                "file",
                "--input-file",
                str(source),
                "--count",
                "2",
                "--seed",
                "42",
                "--drive-root",
                str(root),
                "--exclude-file",
                str(extra),
                "--data-dir",
                str(output),
                "--no-progress",
            ],
        )
        assert result.exit_code == 0, result.output
        assert f"Previously screened packages: {len(rows)}" in result.output
        assert "Additional exclusions: 1" in result.output
        assert "Duplicate packages in output: 0" in result.output
        assert c.files(root) == before and extra.read_bytes() == original_extra
        manifest_path = next((output / "registry/runs").glob("*/manifest.json"))
        manifest = c.read_json(manifest_path)
        selected = set(
            (manifest_path.parent / "targets.txt").read_text(encoding="utf-8").splitlines()
        )
        assert not selected & (rows.keys() | all_selected | {"pypi:new-0"})
        assert manifest["exclusion"]["excluded_name_count"] == len(rows) + 1
        all_selected.update(selected)
        submitted = root / "discovery/submissions" / manifest["sampling_run_id"]
        for name in ("manifest.json", "package_candidates.csv"):
            c.copy_verified(manifest_path.parent / name, submitted / name)
        d.screen_drive(submitted, data_root=tmp_path / "s", client=PyPI())
    assert len(all_selected) == 4


def test_d1_master_change_before_publication_rejects_sample(tmp_path, monkeypatch):
    import reposeer.pipelines.package_discovery as pipeline_module

    root = tmp_path / "drive"
    d.bootstrap_mapping(root)
    source = tmp_path / "names.txt"
    source.write_text("new", encoding="utf-8")
    original = pipeline_module._write_targets

    def changed_master(path, names):
        original(path, names)
        meta = sm.read_master_metadata(root)
        meta["updated_at_utc"] = "2099-01-01T00:00:00.000Z"
        c.write_json(root / "screening/mapping.meta.json", meta, immutable=False)

    monkeypatch.setattr(pipeline_module, "_write_targets", changed_master)
    output = tmp_path / "sample"
    result = CliRunner().invoke(
        app,
        [
            "discover-packages",
            "--source",
            "file",
            "--input-file",
            str(source),
            "--count",
            "1",
            "--drive-root",
            str(root),
            "--data-dir",
            str(output),
        ],
    )
    assert result.exit_code == 1 and "master_changed_retry_sampling" in result.output
    assert not list(output.rglob("manifest.json"))


def test_streaming_master_reader_exceeds_per_run_limits(tmp_path):
    root = tmp_path / "large"
    path = root / "screening/mapping.csv"
    path.parent.mkdir(parents=True)
    run_id = str(uuid4())
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=c.MAPPING_FIELDS, lineterminator="\n")
        writer.writeheader()
        for index in range(10001):
            name = f"pkg-{index:05d}-" + "x" * 280
            writer.writerow(
                dict(
                    sampling_run_id=run_id,
                    package_id="pypi:" + name,
                    package_name=name,
                    repo_full_name="",
                    screening_status="failed",
                    reason_code="pypi_request_failed",
                    source_url=f"https://pypi.org/pypi/{name}/json",
                    retrieved_at_utc="2026-10-23T00:00:00.000Z",
                    evidence_sha256="",
                )
            )
    assert path.stat().st_size > c.MAX_METADATA
    c.write_json(
        path.with_name("mapping.meta.json"),
        dict(
            schema_version=1,
            mapping_sha256=c.fingerprint(path)[0],
            row_count=10001,
            updated_at_utc="2026-10-23T00:00:00.000Z",
            last_merged_run_id=run_id,
        ),
    )
    assert len(sm.read_cumulative_mapping(root)[1]) == 10001


@pytest.mark.parametrize("batches", [[], [["old"]], [["old"], ["new"]]])
def test_bootstrap_recovers_missing_metadata_after_csv_publication(tmp_path, monkeypatch, batches):
    root = tmp_path / "fresh"
    if batches:
        root, _ = historical_drive(tmp_path, batches)
    copy = d.copy_verified

    def interrupted(source, target, **kwargs):
        if target.parent.name == "screening" and target.name == "mapping.meta.json":
            raise OSError("interrupted bootstrap metadata")
        return copy(source, target, **kwargs)

    monkeypatch.setattr(d, "copy_verified", interrupted)
    with pytest.raises(OSError, match="interrupted bootstrap"):
        d.bootstrap_mapping(root)
    assert (root / "screening/mapping.pending.json").exists()
    assert not (root / "screening/mapping.meta.json").exists()
    monkeypatch.setattr(d, "copy_verified", copy)
    assert d.bootstrap_mapping(root)["status"] == "already_initialized"
    assert sm.read_cumulative_mapping(root)[0]["row_count"] == sum(map(len, batches))
    assert not (root / "screening/mapping.pending.json").exists()


@pytest.mark.parametrize(
    "damage",
    [
        "schema",
        "missing",
        "header",
        "checksum",
        "count",
        "canonical",
        "duplicate",
        "unsorted",
        "status",
        "name",
    ],
)
def test_invalid_master_blocks_d1_before_sampling(tmp_path, monkeypatch, damage):
    import reposeer.cli.discover_packages as cli_module

    root = tmp_path / "invalid-master"
    folder = root / "screening"
    folder.mkdir(parents=True)
    run_id = str(uuid4())
    rows = [
        dict(
            sampling_run_id=run_id,
            package_id="pypi:" + name,
            package_name=name,
            repo_full_name="",
            screening_status="failed",
            reason_code="pypi_request_failed",
            source_url=f"https://pypi.org/pypi/{name}/json",
            retrieved_at_utc="2026-10-23T00:00:00.000Z",
            evidence_sha256="",
        )
        for name in ("a", "b")
    ]
    if damage == "canonical":
        rows[0]["package_id"] = "pypi:A"
    elif damage == "duplicate":
        rows[1] = dict(rows[0])
    elif damage == "unsorted":
        rows.reverse()
    elif damage == "status":
        rows[0]["screening_status"] = "verified"
    elif damage == "name":
        rows[0]["package_name"] = "other"
    path = folder / "mapping.csv"
    d.write_drive_table(path, c.MAPPING_FIELDS, rows)
    if damage == "header":
        path.write_bytes(path.read_bytes().replace(b"package_id", b"wrong_field", 1))
    meta = dict(
        schema_version=2 if damage == "schema" else 1,
        mapping_sha256="0" * 64 if damage == "checksum" else c.fingerprint(path)[0],
        row_count=3 if damage == "count" else 2,
        updated_at_utc="2026-10-23T00:00:00.000Z",
        last_merged_run_id=run_id,
    )
    c.write_json(folder / "mapping.meta.json", meta)
    if damage == "missing":
        path.unlink()
    before = c.files(root)
    monkeypatch.setattr(
        cli_module,
        "PackageDiscoveryPipeline",
        lambda **kwargs: pytest.fail("Invalid master started sampling"),
    )
    source = tmp_path / "names.txt"
    source.write_text("new", encoding="utf-8")
    output = tmp_path / "sample"
    result = CliRunner().invoke(
        app,
        [
            "discover-packages",
            "--source",
            "file",
            "--input-file",
            str(source),
            "--count",
            "1",
            "--drive-root",
            str(root),
            "--data-dir",
            str(output),
        ],
    )
    assert result.exit_code == 1 and "master_" in result.output
    assert c.files(root) == before and not output.exists()


def test_v2_policy_limits_and_preflight_before_any_output(tmp_path, environment, monkeypatch):
    root, run, run_id = discovery(tmp_path, ["flask"])
    d.screen_drive(run, data_root=tmp_path / "screen", client=PyPI())
    marker, _, _ = c.verify_drive_screening(root, run_id)
    assert c.screening_policy(marker) == "distributed-v2"
    assert c.collection_options("initial") == c.policy_options("legacy-initial")
    marker["collection_policy"] = {"policy_id": "unknown"}
    with pytest.raises(c.CoordinationError, match="invalid_collection_policy"):
        c.verify_drive_screening(root, run_id, marker=marker)
    assignment = root / f"assignments/C1/{run_id}.csv"
    monkeypatch.setattr(w, "check_providers", lambda **kwargs: dict(ready=False, providers=[]))
    result = w.crawl_drive(assignment, data_root=tmp_path / "worker")
    assert result["status"] == "provider_setup_incomplete"
    assert not (tmp_path / "worker").exists() and not (root / "submissions").exists()
    pipeline, paths, _, _, _ = environment
    pipeline.run(["pypi:flask"], **c.collection_options("initial"))
    checks = []
    monkeypatch.setattr(w, "check_providers", lambda **kwargs: checks.append(kwargs) or dict(ready=True))

    def runner(command, **kwargs):
        for name, value in c.policy_options("distributed-v2").items():
            assert command[command.index("--" + name.replace("_", "-")) + 1] == (
                ",".join(value) if isinstance(value, list) else str(value))
        shutil.copytree(paths.base_dir, Path(command[command.index("--data-dir") + 1]), dirs_exist_ok=True)

    result = w.crawl_drive(assignment, data_root=tmp_path / "worker", runner=runner)
    assert result["failed"] == 0 and len(checks) == 1
    monkeypatch.setattr(w, "check_providers", lambda **kwargs: pytest.fail("READY replay must not preflight"))
    assert w.crawl_drive(assignment, data_root=tmp_path / "fresh")["skipped_existing"] == 1


@pytest.mark.parametrize("scenario", ["documents", "quota", "empty", "republish"])
def test_external_enrichment_immutable_external_only_and_idempotent(tmp_path, environment, monkeypatch, scenario):
    import httpx
    from tests.integration.test_external_crawl_pipeline import HTML

    from reposeer.clients.external.devto import DevToClient
    from reposeer.clients.external.hackernews import HackerNewsClient
    from reposeer.clients.external.news import NewsAPIProvider
    from reposeer.clients.external.rss import RSSProvider
    from reposeer.clients.external.stackexchange import StackExchangeClient
    from reposeer.collection.http.client import HTTPClient
    from reposeer.collectors.external.crawler.safety import URLPolicy
    from reposeer.pipelines.external import ExternalPipeline
    from reposeer.pipelines.external_enrichment import ExternalEnrichmentPipeline

    root, run, run_id = discovery(tmp_path, ["flask"])
    d.screen_drive(run, data_root=tmp_path / "screen", client=PyPI())
    marker_path = root / f"screening/runs/{run_id}/READY.json"
    legacy = c.read_json(marker_path)
    legacy.pop("collection_policy")
    c.write_json(marker_path, legacy, immutable=False)
    pipeline, paths, _, options, _ = environment
    options["fail_provider"] = True
    source_result = pipeline.run(["pypi:flask"], **c.collection_options("initial"))
    assert source_result.status == "partial"

    def runner(command, **kwargs):
        shutil.copytree(paths.base_dir, Path(command[command.index("--data-dir") + 1]), dirs_exist_ok=True)

    assert w.crawl_drive(root / f"assignments/C1/{run_id}.csv", data_root=tmp_path / "crawl", runner=runner)["partial"] == 1
    submissions = root / "submissions/C1"
    source_inventory = c.files(submissions)
    requests, checks = [], []
    monkeypatch.setattr(w, "check_providers", lambda **kwargs: checks.append(kwargs) or dict(ready=True))
    policy = URLPolicy(lambda host: ["93.184.216.34"])

    def handler(request):
        requests.append(request)
        assert request.url.host not in {"api.github.com", "pypi.org"}
        if request.url.host == "hn.algolia.com":
            return httpx.Response(200, json={"hits": [] if scenario == "empty" else [{"objectID": "42", "title": "Flask release",
                "url": "https://blog.example/flask"}], "nbPages": 1})
        if request.url.host == "dev.to":
            return httpx.Response(200, json=[])
        if request.url.host == "api.stackexchange.com":
            return httpx.Response(200, json={"items": [], "has_more": False})
        if request.url.host == "newsapi.org":
            if scenario == "quota":
                return httpx.Response(200, json={"status": "error", "code": "apiKeyExhausted"})
            return httpx.Response(200, json={"status": "ok", "totalResults": 0, "articles": []})
        if request.url.host == "feeds.example":
            return httpx.Response(200, content=b'<rss version="2.0"><channel/></rss>')
        assert request.url.host == "blog.example"
        return httpx.Response(200, text=HTML.replace("synthetic package", "Flask Python package"),
                              headers={"content-type": "text/html"})

    with HTTPClient(transport=httpx.MockTransport(handler), max_retries=1) as http:
        def factory(paths):
            providers = dict(hackernews=HackerNewsClient(http_client=http), devto=DevToClient(http_client=http),
                stackexchange=StackExchangeClient(http_client=http), news=NewsAPIProvider("unit-news", http_client=http),
                rss=RSSProvider(["https://feeds.example/rss"], http_client=http, policy=policy))
            return ExternalEnrichmentPipeline(paths, http_client=http, url_policy=policy,
                discovery_factory=lambda **kwargs: ExternalPipeline(providers=providers, **kwargs))

        output = tmp_path / "enrichment"
        assert w.enrich_drive(submissions, data_root=output, dry_run=True)["planned"] == 1
        assert not requests and not output.exists()
        copy = w.copy_verified
        if scenario == "republish":
            def interrupted_copy(source, target, **kwargs):
                if target.name == "READY.json":
                    raise OSError("simulated publication interruption")
                return copy(source, target, **kwargs)

            monkeypatch.setattr(w, "copy_verified", interrupted_copy)
        result = w.enrich_drive(submissions, data_root=output, pipeline_factory=factory)
        assert c.files(submissions) == source_inventory
        if scenario == "republish":
            assert result["failed"] == 1
            monkeypatch.setattr(w, "copy_verified", copy)
            requests.clear()
            checks.clear()
            result = w.enrich_drive(submissions, data_root=output, pipeline_factory=factory)
            assert not requests and not checks

        if scenario == "quota":
            assert result["partial"] == 1 and result["no_candidates"] == 0
            assert not list((root / "external-enrichments").rglob("READY.json"))
            assert result["providers"]["news"]["failures"] > 0
            assert len([request for request in requests if request.url.host == "newsapi.org"]) == 1
            scenario = "documents"
            requests.clear()
            result = w.enrich_drive(submissions, data_root=output, pipeline_factory=factory)
        assert result["no_candidates" if scenario == "empty" else "completed"] == 1 and result["failed"] == 0, result
        assert result["documents_extracted"] == (0 if scenario == "empty" else 1)
        assert len([request for request in requests if request.url.host == "blog.example"]) == (0 if scenario in {"empty", "republish"} else 1)
        before = c.files(root / "external-enrichments")
        requests.clear()
        checks.clear()
        assert w.enrich_drive(submissions, data_root=output, pipeline_factory=factory)["skipped_existing"] == 1
        assert not requests and not checks
        assert c.files(root / "external-enrichments") == before and c.files(submissions) == source_inventory
        source_ready = next(submissions.rglob("READY.json"))
        source_ready.write_bytes(source_ready.read_bytes() + b" ")
        changed = w.enrich_drive(submissions, data_root=output, pipeline_factory=factory)
        assert changed["failed"] == 1
        assert changed["failures"][0]["error_code"] == "source_fingerprint_mismatch"
