"""Real offline JSONL -> T-018 -> T-019 -> Parquet/DuckDB and CLI handoff."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import polars as pl
import pytest
from tests.unit.processing.test_normalization import (
    activity_raw,
    document_raw,
    repository_raw,
    target,
)
from typer.testing import CliRunner

from reposeer.cli.app import app
from reposeer.pipelines.processed import ProcessedDatasetPipeline
from reposeer.storage.manifest import ProcessingManifest
from reposeer.storage.paths import StoragePaths
from reposeer.storage.query.duckdb import DuckDBQueryEngine
from reposeer.storage.raw.jsonl import JsonlStorage


def manifest(output):
    return ProcessingManifest.model_validate_json(output.manifest_path.read_bytes())


def append(paths, records):
    path = paths.raw_path("github", "repositories")
    JsonlStorage().append_contract_records_durable(path, records)
    return path


def test_observation_revisions_internal_groups_relevance_and_replay(tmp_path):
    paths = StoragePaths(tmp_path)
    first = repository_raw()
    first.payload["forks_count"] = None
    first.payload["archived"] = True
    second = repository_raw(raw_record_id="raw:" + str(uuid4()), repo_id="github:repository:900000002",
                            source_record_id="900000002")
    second.payload.update(id=900000002, full_name="Synthetic/Other")
    excluded = repository_raw(raw_record_id="raw:" + str(uuid4()))
    excluded.payload["fork"] = True
    groups = [activity_raw(name) for name in ("commits", "issues", "pull_requests", "contributors", "releases")]
    groups += [activity_raw("commits", sha="b"*40, commit={}),
               activity_raw("contributors", id=None, login=None, type="Anonymous", name="Anonymous", email="anon@example.test"),
               activity_raw("issues")]
    groups[-3].source_record_id = "commit:" + "b"*40
    groups[-3].event_at = None
    groups[-2].source_record_id = None
    groups[-1].repo_id = second.repo_id
    groups[-1].source_url = "https://api.github.com/repos/Synthetic/Other/issues"
    for observation in (groups[1], groups[2], groups[-1]):
        observation.payload["closed_at"] = "2024-01-02T00:00:00Z"
    documents = [
        document_raw(),
        document_raw("Hiking advice.\n\nReferences:\nFlask is a Python framework."),
        document_raw("Carry water in a flask during the hike."),
        document_raw(),
    ]
    for doc, url in zip(documents[2:], ("https://blog.example/collision", "https://blog.example/copy"), strict=True):
        doc.payload.update(url=url, fetched_url=url)
    documents.append(document_raw())
    raw_path = append(paths, [first, second, excluded, *groups, *documents])
    before = raw_path.read_bytes()
    pipeline = ProcessedDatasetPipeline(paths)
    output = pipeline.run([raw_path], targets=[target()], cutoff_at="2024-01-01T00:00:00.000Z")
    metadata = manifest(output)
    assert output.status == metadata.processing_status == "complete"
    assert output.input_record_count == 16
    assert metadata.source_coverage == "not_verified"
    assert metadata.input_fingerprints[str(raw_path.resolve())] == hashlib.sha256(before).hexdigest()
    assert metadata.processing_summary.internal_only_observation_count == 4
    assert len(metadata.raw_observation_fingerprints) == 16
    repos = pl.read_parquet(paths.processed_path("repositories"))
    assert repos.height == 2 and repos["repo_id"].n_unique() == 2
    assert repos.filter(pl.col("repo_id") == first.repo_id)["forks_count"].to_list() == [None]
    assert repos["is_archived"].to_list().count(True) == 1
    events = pl.read_parquet(paths.processed_path("github_activity"))
    assert events.height == 7 and events["available_at"].null_count() == 7
    assert not set(events["activity_kind"]) & {"contributor", "release"}
    details = pl.read_parquet(paths.processed_path("github_activity_details"))
    assert details.height == 8
    anonymous = details.filter(pl.col("entity_id").str.starts_with("raw:"))
    assert anonymous.height == 1 and json.loads(anonymous["entity_json"][0])["contributor_github_id"] is None
    docs = pl.read_parquet(paths.processed_path("external_documents"))
    assert docs.height == 5 and docs["article_id"].n_unique() == 3
    relevance = pl.read_parquet(paths.processed_path("external_relevance"))
    assert set(relevance["decision"]) == {"relevant", "irrelevant", "unresolved"}
    assert relevance.height == 5
    assert pl.read_parquet(paths.processed_path("external_lineage")).height == 5
    audit = pl.read_parquet(paths.processed_path("processing_audit"))
    assert set(audit["item_type"]) >= {"repository_group", "activity_group", "document_group",
                                          "content_duplicate", "repository_exclusion", "temporal_diagnostic"}
    repo_group = audit.filter(pl.col("item_type") == "repository_group")
    observed_runs = {o["run_id"] for payload in repo_group["payload_json"]
                     for o in json.loads(payload)["observations"]}
    assert first.run_id in observed_runs
    stored_bytes = {name: paths.processed_path(name).read_bytes() for name in output.dataset_row_counts
                    if name != "processing_audit"}
    replay = pipeline.run([raw_path], targets=[target()], cutoff_at="2024-01-01T00:00:00.000Z")
    assert replay.status == "complete"
    assert all(count == 0 for name, count in replay.dataset_row_counts.items() if name != "processing_audit")
    assert all(paths.processed_path(name).read_bytes() == data for name, data in stored_bytes.items())
    assert raw_path.read_bytes() == before
    assert output.manifest_path.parent == tmp_path / "metadata/collection_runs"
    assert all(Path(info.path).is_relative_to(tmp_path) for info in metadata.datasets.values())
    assert not (tmp_path / "features").exists()
    engine = DuckDBQueryEngine()
    try:
        for name, expected in [("repositories", 2), ("github_activity", 7),
                               ("external_documents", 5), ("external_relevance", 5)]:
            engine.register_parquet(name, paths.processed_path(name))
            assert engine.execute_sql(f"SELECT count(*) FROM {name}") == [(expected,)]
    finally:
        engine.close()


def test_partial_input_keeps_errors_and_internal_only_is_complete(tmp_path):
    paths = StoragePaths(tmp_path)
    internal = append(paths, [activity_raw("contributors"), activity_raw("releases")])
    pipeline = ProcessedDatasetPipeline(paths)
    output = pipeline.run([internal])
    assert output.status == "complete" and output.dataset_row_counts["github_activity"] == 0
    assert output.dataset_row_counts["github_activity_details"] == 2
    with internal.open("a", encoding="utf-8") as stream:
        stream.write('{"invalid":"envelope"}\n')
    partial = pipeline.run([internal])
    assert partial.status == "partial" and partial.error_count == 1
    assert manifest(partial).processing_summary.rejected_observation_count == 1
    audit = pl.read_parquet(paths.processed_path("processing_audit"))
    assert "processing_error" in audit["item_type"]
    cli = CliRunner().invoke(app, ["process", "--data-dir", str(tmp_path), "--raw", str(internal)])
    assert cli.exit_code == 1 and "Processing status: partial" in cli.output


@pytest.mark.parametrize("case", ["missing", "invalid_json", "empty", "excluded", "schema"])
def test_failed_inputs_write_failed_manifest(tmp_path, case):
    paths = StoragePaths(tmp_path)
    path = paths.raw_path("github", "repositories")
    if case == "invalid_json":
        path.write_text("invalid", encoding="utf-8")
    elif case == "empty":
        path.touch()
    elif case == "excluded":
        raw = repository_raw()
        raw.payload["fork"] = True
        append(paths, [raw])
    elif case == "schema":
        append(paths, [repository_raw()])
        pl.DataFrame({"legacy": [1]}).write_parquet(paths.processed_path("repositories"))
    with pytest.raises((ValueError, FileNotFoundError)):
        ProcessedDatasetPipeline(paths).run([path])
    saved = list((paths.metadata_dir / "collection_runs").glob("processing_*.json"))
    assert len(saved) == 1
    metadata = ProcessingManifest.model_validate_json(saved[0].read_bytes())
    assert metadata.processing_status == "failed" and metadata.source_coverage == "not_verified"
    if case == "schema":
        assert metadata.datasets["repositories"].total_row_count is None
        assert metadata.datasets["repositories"].status == "failed"


def test_cross_dataset_failure_resumes_without_duplicate_or_secret(tmp_path, monkeypatch):
    paths = StoragePaths(tmp_path)
    raw_path = append(paths, [repository_raw(), activity_raw("commits"), document_raw()])
    pipeline = ProcessedDatasetPipeline(paths)
    write = pipeline.writer.parquet_storage.write

    def fail_activity(path, frame):
        if path.stem == "github_activity":
            raise OSError("sensitive-value")
        write(path, frame)

    monkeypatch.setattr(pipeline.writer.parquet_storage, "write", fail_activity)
    with pytest.raises(OSError):
        pipeline.run([raw_path])
    first_manifest = next((paths.metadata_dir / "collection_runs").glob("processing_*.json"))
    metadata = ProcessingManifest.model_validate_json(first_manifest.read_bytes())
    assert metadata.processing_status == "failed"
    assert metadata.datasets["repositories"].inserted_row_count == 1
    assert metadata.datasets["github_activity"].status == "failed"
    assert "sensitive-value" not in first_manifest.read_text(encoding="utf-8")
    previous = paths.processed_path("repositories").read_bytes()
    monkeypatch.setattr(pipeline.writer.parquet_storage, "write", write)
    output = pipeline.run([raw_path])
    assert output.status == "complete" and output.dataset_row_counts["repositories"] == 0
    assert output.dataset_row_counts["github_activity"] == 1
    assert paths.processed_path("repositories").read_bytes() == previous


def test_raw_uuid_conflict_even_when_native_field_is_not_projected(tmp_path):
    paths = StoragePaths(tmp_path)
    raw = repository_raw()
    original = append(paths, [raw])
    output = ProcessedDatasetPipeline(paths).run([original])
    raw.payload["providerNativeKey"] = "changed but not normalized"
    conflicting = tmp_path / "changed.jsonl"
    JsonlStorage().append_contract_record(conflicting, raw)
    before = paths.processed_path("repositories").read_bytes()
    with pytest.raises(ValueError, match="conflicting_persisted_raw_record_id"):
        ProcessedDatasetPipeline(paths).run([conflicting])
    assert paths.processed_path("repositories").read_bytes() == before
    assert manifest(output).processing_status == "complete"


def test_cli_default_discovery_targets_and_failure_exit(tmp_path):
    paths = StoragePaths(tmp_path)
    raw_path = paths.raw_path("external", "documents")
    JsonlStorage().append_contract_record(raw_path, document_raw())
    paths.raw_path("external", "discovery").write_text("not an article", encoding="utf-8")
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps([target().model_dump(mode="json")]), encoding="utf-8")
    runner = CliRunner()
    output = runner.invoke(app, ["process", "--data-dir", str(tmp_path), "--targets-file", str(targets)])
    assert output.exit_code == 0, output.output
    assert "Processing status: complete" in output.output and "1 files / 1 records" in output.output
    assert paths.processed_path("external_relevance").is_file()
    empty = runner.invoke(app, ["process", "--data-dir", str(tmp_path / "no-input")])
    assert empty.exit_code == 2 and "completed" not in empty.output.lower()
    targets.write_text('[{"package_id":"pypi:flask","invented":true}]', encoding="utf-8")
    invalid = runner.invoke(app, ["process", "--data-dir", str(tmp_path), "--targets-file", str(targets)])
    assert invalid.exit_code == 2
    help_result = runner.invoke(app, ["process", "--help"])
    assert help_result.exit_code == 0 and "--cutoff-at" in help_result.output


def test_manifest_write_failure_is_not_success_and_replay_recovers(tmp_path, monkeypatch):
    import reposeer.storage.manifest as manifests

    paths = StoragePaths(tmp_path)
    raw_path = append(paths, [repository_raw()])
    write = manifests.write_json_atomic
    calls = 0

    def fail_once(path, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("private-error-value")
        write(path, payload)

    monkeypatch.setattr(manifests, "write_json_atomic", fail_once)
    with pytest.raises(OSError):
        ProcessedDatasetPipeline(paths).run([raw_path])
    saved = next((paths.metadata_dir / "collection_runs").glob("processing_*.json"))
    failed = ProcessingManifest.model_validate_json(saved.read_bytes())
    assert failed.processing_status == "failed" and "private-error-value" not in saved.read_text()
    replay = ProcessedDatasetPipeline(paths).run([raw_path])
    assert replay.status == "complete" and replay.dataset_row_counts["repositories"] == 0
