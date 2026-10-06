"""Live smoke tests querying real GitHub repository and piping to DuckDB."""

from pathlib import Path

import pytest

from reposeer.clients.github import GitHubClient
from reposeer.collectors.github.repository import RepositoryCollector
from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.storage.processed.parquet import ParquetStorage
from reposeer.storage.query.duckdb import DuckDBQueryEngine
from reposeer.storage.raw.jsonl import JsonlStorage


@pytest.mark.smoke
def test_smoke_real_github_curl_request():
    """Live smoke test: Curl/fetch real GitHub API repo metadata for octocat/Hello-World."""
    client = GitHubClient()
    try:
        data = client.get_repository("octocat", "Hello-World")
        assert isinstance(data, dict)
        assert data.get("name") == "Hello-World"
        assert data.get("owner", {}).get("login") == "octocat"
        assert data.get("stargazers_count", 0) > 0
        assert "html_url" in data
    finally:
        client.close()


@pytest.mark.smoke
def test_smoke_real_github_commits_fetch():
    """Live smoke test: Query real commit history from GitHub API."""
    client = GitHubClient()
    try:
        commits = client.list_commits("octocat", "Hello-World", per_page=3, page=1)
        assert isinstance(commits, list)
        assert len(commits) > 0
        first_commit = commits[0]
        assert "sha" in first_commit
        assert "commit" in first_commit
        assert "message" in first_commit["commit"]
    finally:
        client.close()


@pytest.mark.smoke
def test_smoke_pipeline_live_to_duckdb(tmp_path: Path):
    """Live smoke pipeline: fetch real repo -> validate Pydantic -> JSONL -> Parquet -> DuckDB query."""
    # 1. Fetch live metadata from GitHub
    collector = RepositoryCollector()
    record = collector.collect("octocat", "Hello-World")

    assert isinstance(record, RepositoryRecord)
    assert record.full_name == "octocat/Hello-World"
    assert record.stars > 0

    # 2. Persist to Raw JSONL
    jsonl = JsonlStorage()
    raw_path = tmp_path / "raw_repositories.jsonl"
    jsonl.append_record(raw_path, record)
    assert raw_path.exists()

    raw_records = jsonl.read(raw_path)
    assert len(raw_records) == 1
    assert raw_records[0]["full_name"] == "octocat/Hello-World"

    # 3. Transform to Processed Parquet
    parquet = ParquetStorage()
    parquet_path = tmp_path / "repositories.parquet"
    parquet.write(parquet_path, raw_records)
    assert parquet_path.exists()

    # 4. Query live through DuckDB SQL Engine
    duckdb_engine = DuckDBQueryEngine()
    try:
        duckdb_engine.register_parquet("github_repos", parquet_path)
        rows = duckdb_engine.execute_sql(
            "SELECT full_name, stars, default_branch, archived FROM github_repos WHERE stars > 0"
        )
        assert len(rows) == 1
        full_name, stars, default_branch, archived = rows[0]
        assert full_name == "octocat/Hello-World"
        assert stars == record.stars
        assert default_branch in ("master", "main")
        assert archived is False
    finally:
        duckdb_engine.close()
