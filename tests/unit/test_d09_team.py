import zipfile
from types import SimpleNamespace

import pytest
from scripts import d09_team as team
from scripts import run_d09_drive as drive
from scripts.audit_d09_readiness import completed_batches
from scripts.d09_archive_pool import ArchivePool
from scripts.d09_team_store import SyncPending, publish, unpack


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(drive.canonical_json(value))


def source_run(path, count=6):
    targets = [
        {"package_id": f"pypi:p{i}", "repo_full_name": f"org/p{i}", "screening_status": "candidate"}
        for i in range(count)
    ]
    frozen = {
        "config": {
            "version": 1,
            "screening_dir": str(path / "screening"),
            "limit": None,
            "batch_size": 1,
        },
        "drive_metadata": {"row_count": count},
        "targets": targets,
        "targets_sha256": drive.digest(drive.canonical_json(targets)),
    }
    packages = {
        row["package_id"]: {
            "package_id": row["package_id"],
            "source_names": [row["package_id"][5:]],
            "first_upload_at": "2019-01-01T00:00:00.000Z",
        }
        for row in targets
    }
    write(path / "drive-targets.json", frozen)
    write(
        path / "historical-registry.json",
        {
            "packages": packages,
            "packages_sha256": drive.digest(drive.canonical_json(packages)),
            "targets_sha256": frozen["targets_sha256"],
            "registry_receipts": {},
        },
    )
    write(path / "inventory/inventory.json", {})
    write(
        path / "archive-inventory.json",
        {
            "path": str(path / "inventory/inventory.json"),
            "sha256": drive.file_digest(path / "inventory/inventory.json"),
        },
    )
    return path


def checkpoint(root, index, *, pending=False):
    frozen, batches = team.frozen_inputs(root)
    packages = [package for group in batches[index] for package in group]
    batch = root / "batches" / f"batch-{index:05d}"
    drive.prepare_batch(batch, packages, {}, frozen)
    write(batch / "shard-0000/review.jsonl", {"package_id": packages[0]["package_id"]})
    revision = drive.shard_reviews_digest(batch)
    write(batch / "collection.json", {"source_reviews_sha256": revision})
    rows = [
        {
            "package_id": packages[0]["package_id"],
            "status": "archive_download_pending" if pending else "mapping_sources_matched",
        }
    ]
    body = drive.jsonl(rows)
    mapping = batch / "verification/mapping_evidence.jsonl"
    mapping.parent.mkdir(parents=True)
    mapping.write_bytes(body)
    write(
        batch / "verification/summary.json",
        {
            "path": "mapping_evidence.jsonl",
            "content_sha256": drive.digest(body),
            "candidate_rows": 1,
            "statuses": {rows[0]["status"]: 1},
            "source_reviews_sha256": revision,
        },
    )
    return batch


@pytest.fixture
def prepared_team(tmp_path, monkeypatch):
    root = source_run(tmp_path / "captain")
    checkpoint(root, 0)
    checkpoint(root, 1)
    readme = tmp_path / "README.md"
    readme.write_text("client fixture")
    monkeypatch.setattr(team, "runtime_files", lambda: {"README.md": readme})
    shared = tmp_path / "shared"
    team.initialize(
        SimpleNamespace(
            shared_dir=shared,
            source_run=root,
            inventory_dir=root / "inventory",
            members=3,
            stop_after_batches=6,
        )
    )
    return root, shared, team.load_plan(shared)


def member_options(path, shared, member):
    return SimpleNamespace(
        work_dir=path,
        shared_dir=shared,
        member=member,
        archive_workers=4,
        max_downloads=200,
        sync_seconds=120,
        include_partials=False,
    )


def test_team_assignments_preserve_completed_work_and_are_disjoint(prepared_team):
    _, shared, plan = prepared_team
    assert plan["seed_completed"] == [0, 1]
    assert plan["assignments"] == {"1": [2, 5], "2": [3], "3": [4]}
    status = team.team_status(shared)
    assert status["completed_batches"] == 2
    assert status["remaining_batches"] == 4


def test_three_members_publish_restore_and_assemble_one_cohort(
    tmp_path, monkeypatch, prepared_team
):
    _, shared, plan = prepared_team
    calls = []

    def collect(options, frozen):
        calls.append(list(options.batch_indices))
        for index in options.batch_indices:
            checkpoint(options.output_dir, index)
        return {
            "status": "completed",
            "mapping_statuses": {"mapping_sources_matched": len(options.batch_indices)},
        }

    monkeypatch.setattr(drive, "run", collect)
    for member in range(1, 4):
        result = team.member_run(member_options(tmp_path / f"worker-{member}", shared, member))
        assert result["state"] == "completed"
    assert calls == [[2, 5], [3], [4]]
    assert team.team_status(shared)["completed_batches"] == 6

    output = tmp_path / "combined"
    result = team.assemble(SimpleNamespace(shared_dir=shared, output_dir=output))
    assert result["completed_batches_restored"] == 6
    batches, pending = completed_batches(output)
    assert len(batches) == 6 and pending == []
    assert team.frozen_inputs(output)[0]["targets_sha256"] == plan["targets_sha256"]


def test_saved_assignment_can_resume_on_another_computer_with_partial_bytes(
    tmp_path, prepared_team
):
    _, shared, plan = prepared_team
    first = member_options(tmp_path / "first-computer", shared, 1)
    first.work_dir.mkdir()
    team.prepare_member(first, plan)
    batch = checkpoint(first.work_dir, 2, pending=True)
    (batch / "verification/example.part").write_bytes(b"batch bytes")
    partial = first.work_dir / "archive-cache/example.json.gz.1.part"
    partial.parent.mkdir(exist_ok=True)
    partial.write_bytes(b"saved archive bytes")
    first.include_partials = True
    team.sync_member(first)

    second = member_options(tmp_path / "second-computer", shared, 1)
    second.work_dir.mkdir()
    team.prepare_member(second, plan)
    assert (
        second.work_dir / "batches/batch-00002/verification/example.part"
    ).read_bytes() == b"batch bytes"
    assert (
        second.work_dir / "archive-cache/example.json.gz.1.part"
    ).read_bytes() == b"saved archive bytes"
    local = second.work_dir / "batches/batch-00002/shard-0000/local-note.txt"
    local.write_text("newer local work")
    team.prepare_member(second, plan)
    assert local.read_text() == "newer local work"


def test_published_archive_proofs_are_reused_by_other_members(tmp_path, prepared_team):
    captain, shared, plan = prepared_team
    folder = captain / "archive-cache"
    line = b'{"repo":{"name":"org/shared","id":123}}\n'
    source = {
        "path": "archives/example.jsonl",
        "content_sha256": drive.digest(line),
        "revision": {"name": "2020-01-01-0.json.gz", "generation": "1"},
        "matched_names": ["org/shared"],
    }
    (folder / "archives").mkdir(parents=True)
    (folder / source["path"]).write_bytes(line)
    write(folder / "archives/example.json", source)
    assert team.publish_cache(captain, shared, plan, 0) == 1
    assert team.publish_cache(captain, shared, plan, 0) == 0
    assert team.pull_cache(shared, captain, plan) == 0
    other = tmp_path / "other"
    other.mkdir()
    assert team.pull_cache(shared, other, plan) == 1
    proofs = list(ArchivePool(other, {"org/shared"}).proofs({"org/shared"}))
    assert len(proofs) == 1
    assert (proofs[0][0] / proofs[0][1]["path"]).read_bytes() == line


def test_active_member_number_cannot_be_reused_on_another_machine(tmp_path, prepared_team):
    _, shared, plan = prepared_team
    options = []
    for name in ("one", "two"):
        args = member_options(tmp_path / name, shared, 1)
        args.work_dir.mkdir()
        team.prepare_member(args, plan)
        options.append(args)
    team.Publisher(options[0], plan).sync()
    with pytest.raises(ValueError, match="active on another computer"):
        team.Publisher(options[1], plan).sync()


def test_failed_cache_import_cleans_long_paths_and_can_retry(tmp_path, monkeypatch, prepared_team):
    captain, shared, plan = prepared_team
    folder = captain / "archive-cache"
    name = "2024-01-01-0.json.gz.1234567890123456-" + "b" * 64 + ".jsonl"
    line = b'{"repo":{"name":"org/shared","id":123}}\n'
    source = {
        "path": f"archives/{name}",
        "content_sha256": drive.digest(line),
        "revision": {"name": "2024-01-01-0.json.gz", "generation": "1234567890123456"},
        "matched_names": ["org/shared"],
    }
    (folder / "archives").mkdir(parents=True)
    (folder / source["path"]).write_bytes(line)
    write(folder / "archives/example.json", source)
    team.publish_cache(captain, shared, plan, 0)
    other = tmp_path / "retry-computer"
    other.mkdir()
    original_unpack = team.unpack

    def interrupted(directory, stage):
        original_unpack(directory, stage)
        raise SyncPending("simulated interruption after writing long cache paths")

    monkeypatch.setattr(team, "unpack", interrupted)
    assert team.pull_cache(shared, other, plan) == 0
    assert not list((other / "archive-cache/team").glob(".import-*"))
    monkeypatch.setattr(team, "unpack", original_unpack)
    assert team.pull_cache(shared, other, plan) == 1
    assert len(list(ArchivePool(other, {"org/shared"}).proofs({"org/shared"}))) == 1


def test_team_bundle_rejects_incomplete_sync_and_modified_payload(tmp_path):
    source = tmp_path / "data.txt"
    source.write_text("checkpoint")
    folder, _ = publish(tmp_path / "shared", {"data.txt": source}, {"kind": "test"})
    (folder / "payload.zip").write_bytes(b"incomplete")
    with pytest.raises(SyncPending):
        unpack(folder, tmp_path / "extracted")
    assert not (tmp_path / "extracted").exists()


def test_team_bundle_rejects_zip_paths_outside_staging(tmp_path):
    folder = tmp_path / "bad"
    folder.mkdir()
    with zipfile.ZipFile(folder / "payload.zip", "w") as archive:
        archive.writestr("../outside.txt", b"bad")
    identity = {
        "version": 1,
        "metadata": {},
        "files": {"../outside.txt": {"sha256": drive.digest(b"bad"), "size": 3}},
    }
    manifest = {
        **identity,
        "bundle_id": drive.digest(drive.canonical_json(identity)),
        "payload_sha256": drive.file_digest(folder / "payload.zip"),
        "payload_size": (folder / "payload.zip").stat().st_size,
    }
    write(folder / "manifest.json", manifest)
    write(folder / "READY.json", {"manifest_sha256": drive.file_digest(folder / "manifest.json")})
    with pytest.raises(ValueError, match="unsafe bundle path"):
        unpack(folder, tmp_path / "extract")
    assert not (tmp_path / "outside.txt").exists()


def test_incomplete_team_setup_cannot_start_a_member(prepared_team):
    _, shared, _ = prepared_team
    (shared / "READY.json").unlink()
    with pytest.raises(SyncPending):
        team.load_plan(shared)


def test_client_update_preserves_checkpoints_and_skips_unchanged_batches(
    tmp_path, monkeypatch, prepared_team
):
    captain, shared, old = prepared_team
    (tmp_path / "README.md").write_text("updated client fixture")
    team.update_client(SimpleNamespace(shared_dir=shared))
    updated = team.load_plan(shared)
    assert updated["runtime_id"] != old["runtime_id"]
    assert team.same_plan({"plan_id": old["plan_id"]}, updated)
    assert updated["assignments"] == old["assignments"]
    assert team.team_status(shared)["completed_batches"] == 2

    def must_not_rescan(*args):
        pytest.fail("unchanged checkpoints should reuse their published bundle")

    monkeypatch.setattr(team, "batch_snapshot", must_not_rescan)
    assert team.publish_batches(captain, shared, updated, 0, [0, 1]) == 2


def test_background_member_can_be_stopped_at_a_saved_pass_boundary(
    tmp_path, monkeypatch, prepared_team
):
    _, shared, _ = prepared_team
    args = member_options(tmp_path / "background-worker", shared, 1)
    calls = []

    def collect(options, frozen):
        calls.append(1)
        team.request_stop(SimpleNamespace(work_dir=options.output_dir))
        return {"status": "resume_to_continue", "mapping_statuses": {"archive_download_pending": 1}}

    monkeypatch.setattr(drive, "run", collect)
    assert team.member_run(args)["state"] == "stopped"
    assert len(calls) == 1
    assert team.team_status(shared)["members"][0]["state"] == "stopped"


def test_runtime_bundle_contains_no_private_environment_files():
    assert all(
        name.endswith(".py") or name in {"README.md", "uv.lock", "pyproject.toml"}
        for name in team.runtime_files()
    )
