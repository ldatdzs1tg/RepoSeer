"""Mounted Drive-only contracts; shared folders and collectors are entirely offline."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from tests.coordination.test_coordination import environment as environment
from tests.coordination.test_coordination import tmp_path as tmp_path
from tools.coordination import _common as c
from tools.coordination import dispatcher as d
from tools.coordination import worker as w

from reposeer.exceptions import HTTPClientError
from reposeer.pipelines.package_discovery import PackageDiscoveryOptions, PackageDiscoveryPipeline


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
        if name == "network":
            raise HTTPClientError("private-provider-body", status_code=429)
        urls = {"Source": "https://github.com/" + self.repositories.get(name, "pallets/flask")}
        if name == "unresolved":
            urls = {"Home": "https://github.com/pallets/flask"}
        if name == "ambiguous":
            urls["Code"] = "https://github.com/other/repository"
        return {"info": {"name": name, "project_urls": urls}}


def discovery(tmp_path, names):
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
    # The convenience mapping remains the most recently completed run on older replay.
    _, newer_run, newer_id = discovery(tmp_path, ["new"])
    d.screen_drive(newer_run, data_root=tmp_path / "s", client=PyPI())
    latest_sha = c.fingerprint(root / f"screening/runs/{newer_id}/mapping.csv")
    d.screen_drive(run, data_root=tmp_path / "s", client=client)
    assert c.fingerprint(root / "screening/mapping.csv") == latest_sha
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
    assert not (root / "screening/mapping.csv").exists()
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
