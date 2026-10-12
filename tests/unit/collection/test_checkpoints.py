"""Run recovery and atomic persistence tests."""

import json

import pytest

from reposeer.collection import CollectionContext, CollectionProgress, RunManager
from reposeer.collection.errors import handle_collection_error
from reposeer.exceptions import CollectionError


def test_checkpoint_round_trip_preserves_start_time_counts_and_cursors(tmp_path):
    manager = RunManager(tmp_path)
    context = manager.start_run("example-run")
    progress = CollectionProgress(context, manager)
    progress.record_success("github.page", 3, count=100)
    progress.record_success("external.html.url", "https://example.test")
    progress.record_error()
    resumed = RunManager(tmp_path).resume_run("example-run")
    assert resumed == context
    assert resumed.started_at == context.started_at
    assert resumed.collected_count == 101
    assert resumed.error_count == 1
    assert resumed.get_checkpoint("github.page") == 3


def test_missing_checkpoint_returns_none_and_existing_run_is_not_overwritten(tmp_path):
    manager = RunManager(tmp_path)
    assert manager.resume_run("missing") is None
    context = manager.start_run("existing")
    with pytest.raises(CollectionError, match="already exists"):
        manager.start_run("existing")
    assert manager.resume_run("existing") == context


@pytest.mark.parametrize("run_id", ["../escape", "..\\escape", "D:\\escape", "", "nested/run"])
def test_checkpoint_run_ids_cannot_escape_directory(tmp_path, run_id):
    manager = RunManager(tmp_path)
    with pytest.raises(ValueError, match="run_id"):
        manager.start_run(run_id)
    with pytest.raises(ValueError, match="run_id"):
        manager.resume_run(run_id)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "damage", ["truncated", "wrong_run", "bad_cursor", "bad_count", "bad_time"]
)
def test_invalid_checkpoint_reports_error_instead_of_starting_fresh(tmp_path, damage):
    manager = RunManager(tmp_path)
    context = manager.start_run("damaged")
    path = manager.save_checkpoint(context)
    data = json.loads(path.read_text(encoding="utf-8"))
    if damage == "truncated":
        path.write_text("{", encoding="utf-8")
    else:
        field, value = {
            "wrong_run": ("run_id", "other"),
            "bad_cursor": ("checkpoint_state", {"page": []}),
            "bad_count": ("collected_count", -1),
            "bad_time": ("started_at", "not a timestamp"),
        }[damage]
        data[field] = value
        path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CollectionError, match="Invalid or unreadable checkpoint"):
        manager.resume_run("damaged")


def test_failed_atomic_replace_preserves_previous_checkpoint_and_cleans_temp_files(
    tmp_path, monkeypatch
):
    manager = RunManager(tmp_path)
    context = manager.start_run("atomic")
    context.set_checkpoint("page", 1)
    path = manager.save_checkpoint(context)
    previous = path.read_bytes()
    context.set_checkpoint("page", 2)

    def fail_replace(source, destination):
        raise OSError("simulated interrupted write")

    monkeypatch.setattr("reposeer.collection.persistence.os.replace", fail_replace)
    with pytest.raises(CollectionError, match="Could not save checkpoint"):
        manager.save_checkpoint(context)
    assert path.read_bytes() == previous
    assert not list(tmp_path.glob("*.tmp"))
    assert manager.resume_run("atomic").get_checkpoint("page") == 1


def test_common_error_handler_records_failure_and_keeps_last_success(tmp_path, caplog):
    manager = RunManager(tmp_path)
    context = manager.start_run("errors")
    progress = CollectionProgress(context, manager)
    progress.record_success("external.html.url", "https://example.test/good")
    handle_collection_error(ValueError("upstream failed"), "external.html", progress=progress)
    restored = manager.resume_run("errors")
    assert restored.collected_count == 1
    assert restored.error_count == 1
    assert restored.get_checkpoint("external.html.url") == "https://example.test/good"
    record = caplog.records[-1]
    assert record.run_id == "errors"
    assert record.entity_id == "external.html"


def test_invalid_progress_does_not_change_context():
    context = CollectionContext()
    progress = CollectionProgress(context)
    with pytest.raises(ValueError):
        progress.record_success("page", 2, count=-1)
    with pytest.raises(ValueError):
        progress.record_success("page", [])
    assert context.collected_count == 0
    assert context.checkpoint_state == {}
