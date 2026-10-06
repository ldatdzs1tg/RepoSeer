"""Unit tests for JSONL, Parquet, and DuckDB storage layers."""

from pathlib import Path

from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.storage.processed.parquet import ParquetStorage
from reposeer.storage.query.duckdb import DuckDBQueryEngine
from reposeer.storage.raw.jsonl import JsonlStorage


def test_jsonl_storage(tmp_path: Path, sample_repo_record: RepositoryRecord):
    jsonl = JsonlStorage()
    target_file = tmp_path / "test_repos.jsonl"

    jsonl.append_record(target_file, sample_repo_record)
    records = jsonl.read(target_file)

    assert len(records) == 1
    assert records[0]["full_name"] == "pallets/flask"
    assert records[0]["stars"] == 65000


def test_parquet_and_duckdb_storage(tmp_path: Path, sample_repo_record: RepositoryRecord):
    parquet = ParquetStorage()
    target_file = tmp_path / "test_repos.parquet"

    parquet.write(target_file, [sample_repo_record.model_dump()])
    df = parquet.read(target_file)

    assert not df.is_empty()
    assert df["full_name"][0] == "pallets/flask"

    # Query with DuckDB
    duck = DuckDBQueryEngine()
    duck.register_parquet("repos_view", target_file)
    results = duck.execute_sql("SELECT stars, full_name FROM repos_view WHERE stars > 50000")

    assert len(results) == 1
    assert results[0][0] == 65000
    assert results[0][1] == "pallets/flask"
    duck.close()
