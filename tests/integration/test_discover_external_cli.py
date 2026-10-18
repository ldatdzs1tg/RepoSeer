"""CLI integration tests for discover-external (Task T-016)."""

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
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
    assert all(raw.repo_id is None and raw.payload["search_target_repo_id"] is None for raw in records)


@pytest.mark.parametrize("damage", [None, "missing", "conflicting_id", "modified", "wrong_repository", "legacy_state"])
def test_cli_discover_external_from_stored(tmp_path: Path, monkeypatch, damage):
    """Resume selects exact raw A despite raw B for the same repo; corrupt/missing A fails closed."""
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
    manifest_file = next((tmp_path / "metadata/collection_runs").glob("discovery_manifest_*.json"))
    run_id = json.loads(manifest_file.read_text(encoding="utf-8"))["run_id"]
    state_file = paths.metadata_dir / "collection_runs" / f"discovery_state_{run_id}.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["scope"]["input_raw_record_id"] == raw_record.raw_record_id
    assert state["scope"]["input_repo_id"] == raw_record.repo_id
    assert state["scope"]["input_observation_fingerprint"]
    other = raw_record.model_copy(update={"raw_record_id": f"raw:{uuid4()}",
                                          "payload": dict(raw_record.payload, description="A newer repository observation")})
    JsonlStorage().append_contract_record(raw_repo_path, other)
    if damage in {"missing", "modified", "wrong_repository"}:
        raw_repo_path.write_text("", encoding="utf-8")
        JsonlStorage().append_contract_record(raw_repo_path, other)
    if damage in {"conflicting_id", "modified", "wrong_repository"}:
        payload = dict(raw_record.payload, description="Altered input snapshot")
        if damage == "wrong_repository":
            payload["full_name"] = "other/repository"
        altered = raw_record.model_copy(update={"payload": payload})
        JsonlStorage().append_contract_record(raw_repo_path, altered)
    elif damage == "legacy_state":
        state.pop("state_format_version")
        state_file.write_text(json.dumps(state), encoding="utf-8")
    elif damage is None:
        # Identical duplicate physical lines are harmless; selection checks their full contents.
        JsonlStorage().append_contract_record(raw_repo_path, raw_record)

    def unexpected_http(*args, **kwargs):
        from reposeer.collection.http.client import HTTPClient

        def handler(request):
            raise AssertionError("Stored resume must not refetch completed jobs or use a different input")

        return HTTPClient(*args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("reposeer.pipelines.external.HTTPClient", unexpected_http)
    original_bytes = discovery_file.read_bytes()
    resumed = runner.invoke(app, ["discover-external", "--from-stored", "--resume", run_id, "--data-dir", str(tmp_path)])
    if damage is not None:
        assert resumed.exit_code == 2, resumed.output
        message = {"missing": "Original upstream RawRecord is unavailable", "legacy_state": "start a new run"}.get(damage, "integrity")
        assert message in resumed.output
        assert discovery_file.read_bytes() == original_bytes
        return
    assert resumed.exit_code == 0, resumed.output
    assert discovery_file.read_bytes() == original_bytes
    replayed = JsonlStorage().read_contract_records(discovery_file)
    assert len(replayed) == len(records)
    assert {raw.raw_record_id for raw in replayed} == {raw.raw_record_id for raw in records}


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


@pytest.mark.parametrize("include_technology_context", [False, True])
def test_cli_explicit_resume_uses_saved_jobs_without_repeating_search(tmp_path, monkeypatch, include_technology_context):
    from reposeer.collection.http.client import HTTPClient

    calls = []
    transport = _mock_hn_transport()

    def handler(request):
        calls.append(request)
        return transport.handle_request(request)

    def custom_http(*args, **kwargs):
        return HTTPClient(*args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("reposeer.pipelines.external.HTTPClient", custom_http)
    runner = CliRunner()
    args = ["discover-external", "octocat/Hello-World", "--data-dir", str(tmp_path)]
    if include_technology_context:
        args.append("--include-technology-context")
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    manifest_file = next((tmp_path / "metadata/collection_runs").glob("discovery_manifest_*.json"))
    run_id = json.loads(manifest_file.read_text(encoding="utf-8"))["run_id"]
    before_resume = len(calls)
    resumed = runner.invoke(app, args + ["--resume", run_id])
    assert resumed.exit_code == 0, resumed.output
    assert len(calls) == before_resume
    unknown = runner.invoke(app, args + ["--sources", "typo"])
    assert unknown.exit_code == 2 and "Unsupported" in unknown.output
