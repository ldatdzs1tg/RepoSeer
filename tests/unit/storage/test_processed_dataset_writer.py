"""Storage conflicts, schema safety, optional tables and DuckDB path handling."""

import hashlib
from uuid import uuid4

import polars as pl
import pytest
from pydantic import ValidationError
from tests.unit.processing.test_normalization import document_raw, repository_raw

from reposeer.pipelines.normalization import NormalizationPipeline
from reposeer.storage.manifest import DatasetWrite
from reposeer.storage.paths import StoragePaths
from reposeer.storage.processed.dataset_writer import DATASET_SCHEMAS, ProcessedDatasetWriter
from reposeer.storage.query.duckdb import DuckDBQueryEngine


def test_writer_observation_keys_and_unprojected_state_conflict(tmp_path):
    writer = ProcessedDatasetWriter(StoragePaths(tmp_path))
    raw = repository_raw()
    result = NormalizationPipeline().process([raw])
    run_id = str(uuid4())
    assert writer.write(result, processing_run_id=run_id)["repositories"] == 1
    before = writer.paths.processed_path("repositories").read_bytes()
    info = writer.dataset_results["repositories"]
    assert info.status == "written"
    assert info.content_sha256 == hashlib.sha256(before).hexdigest()
    assert all(value == 0 for value in writer.write(result, processing_run_id=run_id).values())
    assert writer.paths.processed_path("repositories").read_bytes() == before
    assert writer.dataset_results["repositories"].status == "unchanged"
    assert writer.dataset_results["repositories"].content_sha256 == info.content_sha256
    another = repository_raw(raw_record_id="raw:" + str(uuid4()))
    assert writer.write(NormalizationPipeline().process([raw, another]),
                        processing_run_id=str(uuid4()))["repositories"] == 1
    assert pl.read_parquet(writer.paths.processed_path("repositories")).height == 2
    before = writer.paths.processed_path("repositories").read_bytes()
    raw.payload["description"] = "Changed canonical field absent from the legacy flat projection"
    with pytest.raises(ValueError, match="conflicting_raw_observation_state"):
        writer.write(NormalizationPipeline().process([raw]), processing_run_id=str(uuid4()))
    assert writer.paths.processed_path("repositories").read_bytes() == before


def test_existing_schema_mismatch_does_not_replace_valid_file(tmp_path):
    writer = ProcessedDatasetWriter(StoragePaths(tmp_path))
    path = writer.paths.processed_path("repositories")
    pl.DataFrame({"legacy": [1]}).write_parquet(path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="explicit migration"):
        writer.write(NormalizationPipeline().process([repository_raw()]), processing_run_id=str(uuid4()))
    assert path.read_bytes() == before
    assert writer.dataset_results["repositories"].total_row_count is None
    assert writer.dataset_results["repositories"].status == "failed"
    assert writer.dataset_results["repositories"].content_sha256 is None


def test_optional_empty_datasets_preserve_existing_observations(tmp_path):
    writer = ProcessedDatasetWriter(StoragePaths(tmp_path))
    writer.write(NormalizationPipeline().process([document_raw()]), processing_run_id=str(uuid4()))
    path = writer.paths.processed_path("external_documents")
    before = path.read_bytes()
    assert writer.write(NormalizationPipeline().process([repository_raw()]),
                        processing_run_id=str(uuid4()))["external_documents"] == 0
    assert path.read_bytes() == before
    assert not writer.paths.processed_path("github_activity").exists()
    optional = writer.dataset_results["github_activity"]
    assert optional.relative_path == "processed/github_activity/github_activity.parquet"
    assert optional.content_sha256 is None and optional.total_row_count == 0
    assert pl.read_parquet(path).schema == DATASET_SCHEMAS["external_documents"]


@pytest.mark.parametrize("name", ["bad name", 'x"; DROP TABLE repositories; --', "x.y"])
def test_duckdb_identifier_and_quoted_filesystem_path(tmp_path, name):
    path = tmp_path / "author's records.parquet"
    pl.DataFrame({"value": [7]}).write_parquet(path)
    engine = DuckDBQueryEngine()
    try:
        with pytest.raises(ValueError, match="identifier"):
            engine.register_parquet(name, path)
        engine.register_parquet("safe_view", path)
        assert engine.execute_sql("SELECT value FROM safe_view") == [(7,)]
        with pytest.raises(FileNotFoundError):
            engine.register_parquet("missing", tmp_path / "absent.parquet")
        with pytest.raises(ValueError, match="empty"):
            engine.register_parquet("empty", "")
    finally:
        engine.close()


@pytest.mark.parametrize("relative", ["", ".", "../escape", "/absolute", "C:/data/file", "//host/share", "processed\\file"])
def test_dataset_metadata_rejects_nonportable_paths(relative):
    with pytest.raises(ValidationError, match="dataset_path_must_be_relative_posix"):
        DatasetWrite(path="legacy-path", relative_path=relative, inserted_row_count=0,
                     total_row_count=0, status="unchanged")


def test_writer_rejects_path_outside_root_before_creating_directories(tmp_path):
    paths = StoragePaths(tmp_path / "root")
    paths.processed_dir = tmp_path / "outside"
    writer = ProcessedDatasetWriter(paths)
    with pytest.raises(ValueError, match="dataset_path_outside_storage_root"):
        writer.write(NormalizationPipeline().process([repository_raw()]), processing_run_id=str(uuid4()))
    assert not paths.processed_dir.exists()
