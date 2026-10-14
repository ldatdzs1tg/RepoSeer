"""CLI integration tests for discover-external (Task T-016)."""

import json
from pathlib import Path

import httpx
from typer.testing import CliRunner

from reposeer.cli.app import app
from reposeer.contracts import RawRecord, validate_record
from reposeer.storage.paths import StoragePaths
from reposeer.storage.raw.jsonl import JsonlStorage


def _mock_hn_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if "hn.algolia.com" in request.url.host:
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "objectID": "5001",
                            "title": "Discussion on CLI discovery target",
                            "url": "https://example.com/cli-story",
                            "created_at_i": 1707000000,
                            "points": 12,
                        }
                    ],
                    "nbPages": 1,
                    "page": 0,
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    return httpx.MockTransport(handler)


def test_cli_discover_external_target(tmp_path: Path, monkeypatch):
    """Test CLI discover-external with a direct target."""
    transport = _mock_hn_transport()
    from reposeer.collection.http.client import HTTPClient

    def _custom_http(*args, **kwargs):
        kwargs["transport"] = transport
        return HTTPClient(*args, **kwargs)

    monkeypatch.setattr("reposeer.pipelines.external.HTTPClient", _custom_http)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "discover-external",
            "octocat/Hello-World",
            "--max-pages",
            "1",
            "--data-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Discovering external candidates for: octocat/Hello-World" in result.output
    assert "Query set hash:" in result.output
    assert "Per-Query Discovery Statistics" in result.output

    paths = StoragePaths(tmp_path)
    discovery_file = paths.raw_path("external", "discovery")
    assert discovery_file.exists()
    records = JsonlStorage().read_contract_records(discovery_file)
    assert len(records) > 0


def test_cli_discover_external_from_stored(tmp_path: Path, monkeypatch):
    """Test CLI discover-external reading stored raw repositories."""
    transport = _mock_hn_transport()
    from reposeer.collection.http.client import HTTPClient

    def _custom_http(*args, **kwargs):
        kwargs["transport"] = transport
        return HTTPClient(*args, **kwargs)

    monkeypatch.setattr("reposeer.pipelines.external.HTTPClient", _custom_http)

    paths = StoragePaths(tmp_path)
    sample_file = Path(__file__).resolve().parents[2] / "examples/contracts/github_raw_record.json"
    raw_data = json.loads(sample_file.read_text(encoding="utf-8"))
    raw_record = validate_record(RawRecord.model_validate(raw_data))

    raw_repo_path = paths.raw_path("github", "repositories")
    JsonlStorage().append_contract_record(raw_repo_path, raw_record)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "discover-external",
            "--from-stored",
            "--max-pages",
            "1",
            "--data-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Discovering external candidates for:" in result.output

    discovery_file = paths.raw_path("external", "discovery")
    assert discovery_file.exists()
    records = JsonlStorage().read_contract_records(discovery_file)
    assert len(records) > 0


def test_cli_discover_external_no_args_fails(tmp_path: Path):
    """Test CLI discover-external errors when neither target nor --from-stored is provided."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "discover-external",
            "--data-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    assert "Provide a repository target or specify --from-stored" in result.output
