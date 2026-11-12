import csv
import hashlib
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from scripts import run_d09_drive
from scripts.run_d09_drive import freeze_targets, index_reviews, pack_groups

from reposeer.storage.screening_mapping import MAPPING_FIELDS


def test_freezes_drive_with_complete_repository_group_and_ignores_later_updates(tmp_path):
    screening = tmp_path / "screening"
    output = tmp_path / "run"
    screening.mkdir()
    mapping = screening / "mapping.csv"
    run_id = "00000000-0000-4000-8000-000000000001"
    timestamp = "2026-10-30T00:00:00.000Z"
    with mapping.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, MAPPING_FIELDS)
        writer.writeheader()
        for name in ("alpha", "beta", "gamma"):
            candidate = name != "gamma"
            writer.writerow(
                {
                    "sampling_run_id": run_id,
                    "package_id": f"pypi:{name}",
                    "package_name": name,
                    "repo_full_name": "owner/shared" if candidate else "",
                    "screening_status": "candidate" if candidate else "unresolved",
                    "reason_code": "",
                    "source_url": f"https://pypi.org/pypi/{name}/json",
                    "retrieved_at_utc": timestamp,
                    "evidence_sha256": "f" * 64,
                }
            )
    (screening / "mapping.meta.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mapping_sha256": hashlib.sha256(mapping.read_bytes()).hexdigest(),
                "row_count": 3,
                "updated_at_utc": timestamp,
                "last_merged_run_id": run_id,
            }
        ),
        encoding="utf-8",
    )

    first = freeze_targets(screening, output, {"limit": 1})
    assert [row["package_id"] for row in first["targets"]] == ["pypi:alpha", "pypi:beta"]
    mapping.write_bytes(b"new Drive state")
    assert freeze_targets(screening, output, {"limit": 1}) == first
    with pytest.raises(ValueError, match="resume settings"):
        freeze_targets(screening, output, {"limit": None})


def test_packing_keeps_repository_groups_together():
    groups = [[{"package_id": "a"}, {"package_id": "b"}], [{"package_id": "c"}]]
    assert list(pack_groups(groups, 1)) == [[groups[0]], [groups[1]]]
    assert list(pack_groups(groups, 3)) == [groups]


def test_review_intake_checks_cohort_and_rejects_duplicates(tmp_path):
    path = tmp_path / "reviews.jsonl"
    row = {
        "snapshot": {"package_id": "pypi:alpha", "cutoff_at": "2023-03-31T23:59:59.999Z"},
        "reviewed_at": "2025-01-01T00:00:00.000Z",
    }
    registry = {"pypi:alpha": {"first_upload_at": "2022-01-01T00:00:00.000Z"}}
    end = "2026-01-01T00:00:00.000Z"
    line = json.dumps(row) + "\n"
    path.write_text(line, encoding="utf-8")
    with closing(sqlite3.connect(":memory:")) as db:
        assert index_reviews(db, path, registry, end) == 1
        assert db.execute("SELECT package FROM reviews").fetchall() == [("pypi:alpha",)]
    with closing(sqlite3.connect(":memory:")) as db:
        with pytest.raises(ValueError, match="out-of-cohort"):
            index_reviews(db, path, {}, end)
    path.write_text(line * 2, encoding="utf-8")
    with closing(sqlite3.connect(":memory:")) as db:
        with pytest.raises(ValueError, match="duplicate review"):
            index_reviews(db, path, registry, end)


def test_continuous_mode_stops_when_passes_make_no_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["run_d09_drive", "--all", "--continuous", "--output-dir", str(tmp_path)]
    )
    monkeypatch.setattr(
        run_d09_drive,
        "freeze_targets",
        lambda *args: {"targets": [], "drive_metadata": {"row_count": 0}},
    )
    monkeypatch.setattr(run_d09_drive.time, "sleep", lambda seconds: None)
    calls = []

    def pass_without_progress(*args):
        calls.append(None)
        return {
            "status": "resume_to_continue",
            "batches_processed": 1,
            "mapping_statuses": {"download_budget_reached": 1},
            "stop_reason": "archive_download_budget_reached",
        }

    monkeypatch.setattr(run_d09_drive, "run", pass_without_progress)
    assert run_d09_drive.main() == 0
    assert len(calls) == 2


@pytest.mark.parametrize("collect_only", [False, True])
def test_continuous_mode_exits_at_total_batch_limit(tmp_path, monkeypatch, collect_only):
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_d09_drive",
            "--all",
            "--continuous",
            "--stop-after-batches",
            "2",
            "--output-dir",
            str(tmp_path),
            *(["--collect-only"] if collect_only else []),
        ],
    )

    def freeze(_screening, _output, config):
        assert "stop_after_batches" not in config
        return {"targets": [], "drive_metadata": {"row_count": 0}}

    monkeypatch.setattr(run_d09_drive, "freeze_targets", freeze)
    monkeypatch.setattr(run_d09_drive.time, "sleep", lambda seconds: None)
    calls = []

    def limited_pass(args, _frozen):
        assert args.stop_after_batches == 2
        calls.append(None)
        assert len(calls) <= 2
        stopped = len(calls) == 2
        return {
            "status": "stopped_at_batch_limit" if stopped else "resume_to_continue",
            "batches_processed": 2,
            "mapping_statuses": {"mapping_sources_matched": len(calls)},
            "stop_reason": "batch_limit_reached" if stopped else "batch_budget_reached",
        }

    monkeypatch.setattr(run_d09_drive, "run_collection" if collect_only else "run", limited_pass)
    assert run_d09_drive.main() == 0
    assert len(calls) == 2


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_total_batch_limit_must_be_positive(monkeypatch, limit):
    monkeypatch.setattr("sys.argv", ["run_d09_drive", "--all", "--stop-after-batches", limit])
    with pytest.raises(SystemExit) as result:
        run_d09_drive.main()
    assert result.value.code == 2


@pytest.mark.parametrize("workers", [16, 17])
def test_archive_worker_limit_accepts_sixteen_and_rejects_more(tmp_path, monkeypatch, workers):
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_d09_drive",
            "--all",
            "--plan-only",
            "--output-dir",
            str(tmp_path),
            "--archive-workers",
            str(workers),
        ],
    )
    monkeypatch.setattr(
        run_d09_drive,
        "freeze_targets",
        lambda *args: {"targets": [], "drive_metadata": {"row_count": 0}},
    )
    if workers == 16:
        assert run_d09_drive.main() == 0
    else:
        with pytest.raises(SystemExit) as result:
            run_d09_drive.main()
        assert result.value.code == 2


def test_registry_reuses_valid_page_then_queries_only_remaining_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(run_d09_drive, "REGISTRY_PAGE_SIZE", 2)
    monkeypatch.setattr(run_d09_drive, "REGISTRY_FILTER_SQL_LIMIT", 1)
    projects = {"aardvark", "alpha", "azure"}
    raw = run_d09_drive.jsonl([{"project": "aardvark"}, {"project": "abacus"}])
    (tmp_path / "sources").mkdir()
    path = "sources/old.jsonl"
    (tmp_path / path).write_bytes(raw)
    query = run_d09_drive.registry_query("a", projects, limit=2)
    (tmp_path / "sources/registry-a-000.json").write_text(
        json.dumps(
            {
                "path": path,
                "content_sha256": run_d09_drive.digest(raw),
                "query_sha256": run_d09_drive.digest(query.encode()),
            }
        ),
        encoding="utf-8",
    )

    def lookup(http, output, name, sql, *, post):
        assert post and name == "registry-a-targets-000"
        assert "project IN ('alpha', 'azure')" in sql
        return [{"project": "azure"}]

    monkeypatch.setattr(run_d09_drive, "query_rows", lookup)
    pages = list(run_d09_drive.registry_pages(None, tmp_path, "a", projects))
    assert [name for name, _ in pages] == ["registry-a-000", "registry-a-targets-000"]


def test_waits_for_saved_provider_reset_only_in_continuous_mode(tmp_path, monkeypatch):
    (tmp_path / "provider-quota.json").write_text(
        '{"retry_at_utc":"1970-01-01T00:01:00.000Z"}', encoding="utf-8"
    )
    monkeypatch.setattr(run_d09_drive.time, "time", lambda: 35)
    delays = []
    monkeypatch.setattr(run_d09_drive.time, "sleep", delays.append)
    assert run_d09_drive.wait_for_provider(tmp_path, continuous=False) is False
    assert run_d09_drive.wait_for_provider(tmp_path, continuous=True) is True
    assert delays == [25]


def test_collection_continues_past_a_batch_waiting_for_archive_verification(tmp_path, monkeypatch):
    args = SimpleNamespace(output_dir=tmp_path, max_batches=1)
    packages = [{"package_id": f"pypi:{name}"} for name in ("alpha", "beta")]
    groups = [[[package]] for package in packages]
    monkeypatch.setattr(
        run_d09_drive, "collection_plan", lambda *args: ({"alpha": {}, "beta": {}}, {}, groups)
    )
    monkeypatch.setattr(run_d09_drive, "prepare_batch", lambda *args: None)

    def collect(http, directory, groups):
        receipt = {
            "source_reviews_sha256": run_d09_drive.shard_reviews_digest(directory),
            "mapping_review": {"single_id_observed": 4},
            "package_cutoffs": 4,
            "official_leads": 1,
        }
        run_d09_drive.write_json_atomic(directory / "collection.json", receipt)
        return receipt

    monkeypatch.setattr(run_d09_drive, "collect_batch", collect)
    blocked = tmp_path / "batches/batch-00000/verification/summary.json"
    run_d09_drive.write_json_atomic(blocked, {"statuses": {"download_budget_reached": 4}})
    first = run_d09_drive.run_collection(args, {})
    assert first["batches_processed"] == 1 and first["status"] == "resume_to_continue"
    second = run_d09_drive.run_collection(args, {})
    assert second["batches_processed"] == 2 and second["status"] == "completed"
    assert second["official_leads"] == 2
    assert json.loads(blocked.read_bytes())["statuses"] == {"download_budget_reached": 4}


def test_collection_total_limit_counts_cached_batches_and_can_be_raised(tmp_path, monkeypatch):
    args = SimpleNamespace(output_dir=tmp_path, max_batches=1, stop_after_batches=2)
    packages = [{"package_id": f"pypi:{name}"} for name in ("alpha", "beta", "gamma")]
    groups = [[[package]] for package in packages]
    monkeypatch.setattr(run_d09_drive, "collection_plan", lambda *args: ({}, {}, groups))
    monkeypatch.setattr(run_d09_drive, "prepare_batch", lambda *args: None)
    collected = []

    def collect(_http, directory, _groups):
        collected.append(directory.name)
        receipt = {
            "source_reviews_sha256": run_d09_drive.shard_reviews_digest(directory),
            "mapping_review": {"single_id_observed": 1},
            "package_cutoffs": 1,
            "official_leads": 0,
        }
        run_d09_drive.write_json_atomic(directory / "collection.json", receipt)
        return receipt

    monkeypatch.setattr(run_d09_drive, "collect_batch", collect)
    assert run_d09_drive.run_collection(args, {})["status"] == "resume_to_continue"
    limited = run_d09_drive.run_collection(args, {})
    assert limited["status"] == "stopped_at_batch_limit"
    assert limited["batches_processed"] == 2 and limited["batches_total"] == 3
    assert limited["stop_reason"] == "batch_limit_reached"
    assert run_d09_drive.run_collection(args, {})["status"] == "stopped_at_batch_limit"
    assert collected == ["batch-00000", "batch-00001"]

    args.stop_after_batches = 3
    assert run_d09_drive.run_collection(args, {})["status"] == "completed"
    assert collected == ["batch-00000", "batch-00001", "batch-00002"]


def test_verification_finishes_pending_archives_at_limit_and_resumes_after_it(
    tmp_path, monkeypatch
):
    inventory = tmp_path / "inventory"
    run_d09_drive.write_json_atomic(inventory / "inventory.json", {})
    args = SimpleNamespace(
        output_dir=tmp_path,
        inventory_dir=inventory,
        reviews=tmp_path / "reviews.jsonl",
        max_downloads=30,
        max_batches=5,
        stop_after_batches=1,
        retry_unverified=False,
    )
    packages = [{"package_id": f"pypi:{name}"} for name in ("alpha", "beta")]
    groups = [[[package]] for package in packages]
    registry = {row["package_id"]: {} for row in packages}
    monkeypatch.setattr(run_d09_drive, "collection_plan", lambda *args: (registry, {}, groups))
    monkeypatch.setattr(run_d09_drive, "archive_hints", lambda *args: set())
    monkeypatch.setattr(
        run_d09_drive, "ArchivePool", lambda *args: SimpleNamespace(names=set(), receipts={})
    )
    monkeypatch.setattr(run_d09_drive, "index_reviews", lambda *args: 0)
    monkeypatch.setattr(run_d09_drive, "prepare_batch", lambda *args: None)
    monkeypatch.setattr(run_d09_drive, "collection_checkpoint", lambda *args: {})
    monkeypatch.setattr(
        run_d09_drive, "reviewed_batch", lambda directory, *args: directory / "reviewed"
    )
    monkeypatch.setattr(
        run_d09_drive, "audit_run", lambda *args: {"status": "checked", "input_strict_rows": 0}
    )
    verified, checkpoints = [], {}
    monkeypatch.setattr(
        run_d09_drive,
        "verification_checkpoint",
        lambda directory, _retry: checkpoints.get(directory),
    )

    def verify(directory, _args, _inventory, _budget):
        verified.append(directory.name)
        pending = len(verified) == 1
        receipt = {
            "statuses": {"archive_download_pending" if pending else "mapping_sources_matched": 1}
        }
        if not pending:
            checkpoints[directory] = receipt
        return receipt, 1

    monkeypatch.setattr(run_d09_drive, "verify_mapping", verify)
    frozen = {"targets": packages, "drive_metadata": {"row_count": len(packages)}}
    pending = run_d09_drive.run(args, frozen)
    assert pending["status"] == "resume_to_continue"
    assert pending["stop_reason"] == "archive_downloads_pending"

    limited = run_d09_drive.run(args, frozen)
    assert limited["status"] == "stopped_at_batch_limit"
    assert limited["stop_reason"] == "batch_limit_reached"
    assert limited["batches_processed"] == 1 and limited["batches_total"] == 2
    assert verified == ["batch-00000", "batch-00000"]

    args.stop_after_batches = 2
    assert run_d09_drive.run(args, frozen)["status"] == "completed"
    assert verified == ["batch-00000", "batch-00000", "batch-00001"]


def test_archive_hints_reuse_cached_names_until_collection_revision_changes(tmp_path):
    directory = tmp_path / "batches/batch-00000"
    path = directory / "shard-00000/review.jsonl"
    run_d09_drive.write_json_atomic(directory / "collection.json", {"source_reviews_sha256": "old"})
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(
        run_d09_drive.jsonl([{"explicit_source_urls": ["https://github.com/old/repo"]}])
    )
    targets = [{"repo_full_name": "Current/Repo"}]
    assert run_d09_drive.archive_hints(tmp_path, targets) == {"current/repo", "old/repo"}
    path.write_bytes(
        run_d09_drive.jsonl([{"explicit_source_urls": ["https://github.com/new/repo"]}])
    )
    assert run_d09_drive.archive_hints(tmp_path, targets) == {"current/repo", "old/repo"}
    run_d09_drive.write_json_atomic(directory / "collection.json", {"source_reviews_sha256": "new"})
    assert run_d09_drive.archive_hints(tmp_path, targets) == {"current/repo", "new/repo"}
