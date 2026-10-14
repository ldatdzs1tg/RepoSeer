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



def test_offline_vertical_smoke_uses_mock_transport_and_replays_canonical_candidates(tmp_path):
    import importlib.util

    from reposeer.contracts import RawRecord, validate_record

    spec = importlib.util.spec_from_file_location("vertical_smoke", Path(__file__).resolve().parents[2] / "scripts/smoke_test.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    transport = smoke.offline_transport("Synthetic/Demo")
    calls = []

    def handler(request):
        calls.append(request)
        return transport.handle_request(request)

    import httpx

    summary = smoke.run_smoke_test("Synthetic/Demo", data_dir=tmp_path, transport=httpx.MockTransport(handler))
    assert summary["status"] == "complete"
    assert summary["github_raw_records"] == summary["external_raw_records"] == summary["processed_repositories"] == 1
    assert len(calls) == 2 and calls[1].url.params["query"] == summary["query"]
    raw = JsonlStorage().read_contract_records(tmp_path / "raw/external/discovery.jsonl")[0]
    assert not (tmp_path / "raw/external/articles.jsonl").exists()
    assert isinstance(validate_record(raw), RawRecord)
    assert raw.source == "web_article" and raw.source_record_type == "article"
    assert raw.source_url == "https://example.org/synthetic-story"
    assert raw.payload["title"] == "Synthetic discovery candidate"
    assert raw.payload["query"] == summary["query"]
    assert {key: raw.payload[key] for key in (
        "record_stage", "discovery_status", "content_status", "discovery_provider",
    )} == {"record_stage": "discovery_candidate", "discovery_status": "unverified",
          "content_status": "not_fetched", "discovery_provider": "hackernews"}
    assert raw.payload["source"] == "hackernews" and raw.payload["url"] == raw.source_url
    assert raw.payload["score"] is None and raw.payload["publication_date"] is None
    assert {"body", "text", "article_body", "extracted_text", "content"}.isdisjoint(raw.payload)
    assert raw.repo_id is raw.package_id is raw.event_at is raw.available_at is None
    assert raw.http_status is raw.raw_body_sha256 is None
    github = JsonlStorage().read_contract_records(tmp_path / "raw/github/repositories.jsonl")[0]
    assert raw.run_id == github.run_id
    assert raw.retrieved_at == "2024-02-29T12:00:00.000Z"

    def no_hits(request):
        return httpx.Response(200, json={"hits": []}) if request.url.host == "hn.algolia.com" else transport.handle_request(request)

    empty = smoke.run_smoke_test("Synthetic/Demo", data_dir=tmp_path / "empty", transport=httpx.MockTransport(no_hits))
    assert empty["status"] == "external_unavailable" and empty["external_raw_records"] == 0
    assert len(calls) == 2  # No extra requests were made by replay.
    assert not (tmp_path / "empty/raw/external/articles.jsonl").exists()
    assert JsonlStorage().read_contract_records(tmp_path / "empty/raw/external/discovery.jsonl") == []
