import csv
import hashlib
import json
import sqlite3
from contextlib import closing

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
