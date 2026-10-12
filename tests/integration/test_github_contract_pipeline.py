"""Offline canonical vertical slice, including T-012 shared HTTP ownership."""

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import httpx
import polars as pl
import pytest

from reposeer.clients.github import GitHubClient
from reposeer.collection import HTTPClient
from reposeer.contracts import (
    CollectionRequest,
    GitHubRepository,
    NormalizationRequest,
    validate_collection_pair,
    validate_normalization_pair,
    validate_record,
)
from reposeer.contracts.adapters import (
    GitHubRepositoryContractCollector,
    GitHubRepositoryContractNormalizer,
)
from reposeer.exceptions import CollectionError, HTTPClientError, RateLimitExceededError
from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.services.repository_service import RepositoryService
from reposeer.storage.paths import StoragePaths
from reposeer.storage.query.duckdb import DuckDBQueryEngine
from reposeer.storage.raw.jsonl import JsonlStorage

ROOT = Path(__file__).resolve().parents[2]


def example(name):
    return json.loads((ROOT / "examples/contracts" / (name + ".json")).read_text(encoding="utf-8"))


class FakeGitHub:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else example("github_raw_record")["payload"]
        self.calls = []

    def get_repository(self, owner, repo):
        self.calls.append((owner, repo))
        return deepcopy(self.payload)


def normalization(raw):
    data = example("normalization_request")
    data["run_id"] = raw.run_id
    data["records"] = [raw.model_dump(mode="json")]
    return validate_record(data)


def test_native_json_to_raw_replay_canonical_parquet_and_duckdb(tmp_path):
    fake = FakeGitHub()
    service = RepositoryService(fake, storage_paths=StoragePaths(tmp_path))
    repository = service.collect_and_store("Synthetic", "Demo")
    assert isinstance(repository, GitHubRepository)
    assert fake.calls == [("Synthetic", "Demo")]
    raw_path = tmp_path / "raw/github/repositories.jsonl"
    raw = service.raw_storage.read_contract_records(raw_path)[0]
    assert raw.payload == fake.payload
    assert raw.payload["archived"] is False
    assert raw.payload["full_name"] == "Synthetic/Demo"
    assert raw.payload["open_issues_count"] == 2
    assert "is_archived" not in raw.payload
    assert raw.raw_body_sha256 is None and raw.http_status is None
    assert raw.event_at is None and raw.available_at is None and raw.package_id is None
    assert raw.repo_id == repository.identity.repo_id == "github:repository:900000001"
    request = normalization(raw)
    replay = GitHubRepositoryContractNormalizer().normalize(request)
    validate_normalization_pair(request, replay)
    assert replay.records == [repository]
    assert fake.calls == [("Synthetic", "Demo")]
    assert repository.provenance.raw_record_ids == [raw.raw_record_id]
    assert repository.provenance.valid_at == repository.provenance.available_at == raw.retrieved_at
    assert repository.updated_at == "2024-02-01T00:00:00.000Z"
    parquet = tmp_path / "processed/repositories/repositories.parquet"
    frame = pl.read_parquet(parquet)
    assert frame.schema["forks_count"] == pl.Int64
    assert frame["repo_id"][0] == raw.repo_id
    assert "payload" not in frame.columns and "archived" not in frame.columns
    engine = DuckDBQueryEngine()
    try:
        engine.register_parquet("repositories", parquet)
        assert engine.execute_sql(
            "SELECT repo_id, github_repo_id, repo_full_name, is_archived, is_fork, open_issues_and_prs_count, raw_record_id FROM repositories"
        ) == [(raw.repo_id, "900000001", "Synthetic/Demo", False, False, 2, raw.raw_record_id)]
    finally:
        engine.close()


def test_collection_pair_and_t012_shared_session_preserve_native_values(tmp_path, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=FakeGitHub().payload)

    request = validate_record(example("collection_request"))
    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        with GitHubClient(token="synthetic-secret-token", http_client=http) as client:
            result = GitHubRepositoryContractCollector(client).collect(request)
            validate_collection_pair(request, result)
            service = RepositoryService(client, storage_paths=StoragePaths(tmp_path))
            service.collect_and_store("Synthetic", "Demo")
            service.close()
        # Both the adapter and service leave caller-owned HTTP usable.
        assert http.get("https://example.test/ping").status_code == 200
    assert len(calls) == 3
    assert calls[0].headers["authorization"] == "Bearer synthetic-secret-token"
    assert "authorization" not in calls[-1].headers
    raw_path = tmp_path / "raw/github/repositories.jsonl"
    raw = JsonlStorage().read_contract_records(raw_path)[0]
    replay_request = normalization(raw)
    replay = GitHubRepositoryContractNormalizer().normalize(replay_request)
    validate_normalization_pair(replay_request, replay)
    assert replay.status == "complete"
    assert "synthetic-secret-token" not in raw_path.read_text(encoding="utf-8") + caplog.text
    assert result.records[0].raw_body_sha256 is None


@pytest.mark.parametrize(
    "status,code,exception",
    [
        (404, "not_found", HTTPClientError),
        (403, "http_error", HTTPClientError),
        (401, "http_error", HTTPClientError),
        (429, "rate_limited", RateLimitExceededError),
        (503, "http_error", HTTPClientError),
    ],
)
def test_adapter_classifies_errors_without_changing_t012_exceptions(
    status, code, exception, caplog
):
    request = validate_record(example("collection_request"))
    with HTTPClient(
        max_retries=1,
        clock=lambda: 0,
        sleep=lambda delay: None,
        transport=httpx.MockTransport(
            lambda req: httpx.Response(status, text="secret-response-body")
        ),
    ) as http:
        with GitHubClient(http_client=http) as client:
            result = GitHubRepositoryContractCollector(client).collect(request)
            validate_collection_pair(request, result)
            assert result.status == "failed" and result.records == []
            assert result.errors[0].code == code
            assert result.metadata.coverage[0].status == "unavailable"
            with pytest.raises(exception) as error:
                client.get_repository("Synthetic", "Demo")
            assert error.value.status_code == status
            assert error.value.response_body == "secret-response-body"
    assert "secret-response-body" not in caplog.text + result.model_dump_json()


def test_network_and_invalid_json_errors_are_structured():
    request = validate_record(example("collection_request"))

    def timeout(req):
        raise httpx.ConnectTimeout("synthetic timeout", request=req)

    for handler, code in (
        (timeout, "network_error"),
        (lambda req: httpx.Response(200, text="not JSON"), "parse_error"),
    ):
        with HTTPClient(max_retries=1, transport=httpx.MockTransport(handler)) as http:
            with GitHubClient(http_client=http) as client:
                result = GitHubRepositoryContractCollector(client).collect(request)
                assert result.errors[0].code == code
                validate_collection_pair(request, result)


@pytest.mark.parametrize(
    "field,value", [("id", 1.0), ("id", "900000001"), ("id", True), ("id", 0), ("id", 900000002)]
)
def test_invalid_or_mismatched_ids_do_not_emit_raw(field, value):
    fake = FakeGitHub()
    fake.payload[field] = value
    request = validate_record(example("collection_request"))
    result = GitHubRepositoryContractCollector(fake).collect(request)
    validate_collection_pair(request, result)
    assert result.status == "failed" and result.records == []
    assert result.errors[0].code == "validation_error"


@pytest.mark.parametrize(
    "change,code",
    [
        ("source", "unsupported_record_type"),
        ("kind", "unsupported_record_type"),
        ("multiple", "unsupported_record_type"),
        ("historical", "insufficient_history"),
        ("cutoff", "insufficient_history"),
        ("package", "not_mapped"),
        ("cursor", "unsupported_record_type"),
        ("url_mismatch", "parse_error"),
    ],
)
def test_unsupported_scope_is_honest_and_never_fetches(change, code):
    fake = FakeGitHub()
    data = example("collection_request")
    if change == "source":
        data["source"] = "pypi_json"
    elif change == "kind":
        data["record_kinds"] = ["article"]
    elif change == "multiple":
        data["subjects"] *= 2
    elif change == "historical":
        data["time_range"] = {
            "start_at": "2023-01-01T00:00:00.000Z",
            "end_at": "2024-01-01T00:00:00.000Z",
        }
    elif change == "cutoff":
        data["cutoff_at"] = "2024-01-01T00:00:00.000Z"
    elif change == "package":
        data["subjects"] = [
            {"repo_id": None, "repo_full_name": None, "package_id": "pypi:demo", "url": None}
        ]
    elif change == "cursor":
        data["cursor"] = "next"
    else:
        data["subjects"][0]["url"] = "https://github.com/other/repo"
    request = CollectionRequest.model_validate(data)
    result = GitHubRepositoryContractCollector(fake).collect(request)
    validate_collection_pair(request, result)
    assert result.errors[0].code == code and result.records == []
    assert result.metadata.pages_fetched == 0
    assert not fake.calls


@pytest.mark.parametrize(
    "url", ["https://github.com/Synthetic/Demo", "https://github.com/Synthetic/Demo/"]
)
def test_url_subject_and_callable_injection(url):
    data = example("collection_request")
    data["subjects"][0].update(repo_full_name=None, url=url)
    request = validate_record(data)
    result = GitHubRepositoryContractCollector(FakeGitHub().get_repository).collect(request)
    validate_collection_pair(request, result)
    assert result.status == "complete"


def test_unknown_count_stays_null_in_jsonl_parquet_and_duckdb(tmp_path):
    fake = FakeGitHub()
    del fake.payload["stargazers_count"]
    service = RepositoryService(fake, storage_paths=StoragePaths(tmp_path))
    repository = service.collect_and_store("Synthetic", "Demo")
    assert repository.stargazers_count is None and repository.forks_count == 0
    assert "stargazers_count" in {
        field.field_path for field in repository.provenance.missing_fields
    }
    parquet = tmp_path / "processed/repositories/repositories.parquet"
    frame = pl.read_parquet(parquet)
    assert frame["stargazers_count"][0] is None and frame.schema["stargazers_count"] == pl.Int64
    engine = DuckDBQueryEngine()
    try:
        engine.register_parquet("repositories", parquet)
        assert engine.execute_sql("SELECT stargazers_count, forks_count FROM repositories") == [
            (None, 0)
        ]
    finally:
        engine.close()
    fake.payload["stargazers_count"] = 4
    service.collect_and_store("Synthetic", "Demo")
    assert pl.read_parquet(parquet)["stargazers_count"].to_list() == [None, 4]
    assert (
        len(service.raw_storage.read_contract_records(tmp_path / "raw/github/repositories.jsonl"))
        == 2
    )


@pytest.mark.parametrize(
    "change",
    [
        "created_at",
        "updated_at",
        "fork",
        "archived",
        "is_template",
        "visibility",
        "count_string",
        "naive_timestamp",
        "future_updated",
    ],
)
def test_bad_native_records_persist_raw_but_never_write_processed(tmp_path, change):
    fake = FakeGitHub()
    if change == "visibility":
        fake.payload.pop("visibility")
        fake.payload.pop("private")
    elif change == "count_string":
        fake.payload["stargazers_count"] = "0"
    elif change == "naive_timestamp":
        fake.payload["created_at"] = "2020-01-01T00:00:00"
    elif change == "future_updated":
        fake.payload["updated_at"] = "2999-01-01T00:00:00Z"
    else:
        fake.payload.pop(change)
    service = RepositoryService(fake, storage_paths=StoragePaths(tmp_path))
    with pytest.raises(CollectionError):
        service.collect_and_store("Synthetic", "Demo")
    assert (
        len(service.raw_storage.read_contract_records(tmp_path / "raw/github/repositories.jsonl"))
        == 1
    )
    assert not (tmp_path / "processed/repositories/repositories.parquet").exists()


def test_mixed_normalization_reports_partial_and_unsupported_kind():
    data = example("normalization_request")
    extra = example("article_raw_record")
    data["records"].append(extra)
    request = NormalizationRequest.model_validate(data)
    result = GitHubRepositoryContractNormalizer().normalize(request)
    validate_normalization_pair(request, result)
    assert result.status == "partial"
    assert result.processed_record_count == result.rejected_record_count == 1
    assert result.errors[0].code == "unsupported_record_type"


def test_strict_jsonl_append_roundtrip_and_validation(tmp_path):
    store = JsonlStorage()
    path = tmp_path / "raw.jsonl"
    raw = validate_record(example("github_raw_record"))
    store.append_contract_record(path, raw)
    previous = path.read_bytes()
    store.append_contract_record(path, raw)
    assert path.read_bytes() == previous + previous
    assert store.read_contract_records(path) == [raw, raw]
    assert raw.payload == example("github_raw_record")["payload"]
    with pytest.raises(TypeError):
        store.append_contract_record(path, RepositoryRecord(owner="a", name="b", full_name="a/b"))
    assert path.read_bytes() == previous + previous
    raw.http_status = "200"
    with pytest.raises(ValueError):
        store.append_contract_record(path, raw)
    path.write_text(json.dumps(example("github_repository")) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        store.read_contract_records(path)
    path.write_text(json.dumps({"full_name": "legacy/repo"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        store.read_contract_records(path)


def test_invalid_normalization_preserves_previous_processed_file(tmp_path):
    fake = FakeGitHub()
    service = RepositoryService(fake, storage_paths=StoragePaths(tmp_path))
    service.collect_and_store("Synthetic", "Demo")
    path = tmp_path / "processed/repositories/repositories.parquet"
    previous = path.read_bytes()
    fake.payload.pop("archived")
    with pytest.raises(CollectionError):
        service.collect_and_store("Synthetic", "Demo")
    assert path.read_bytes() == previous


def test_pipeline_uses_service_once_and_propagates_errors(tmp_path, monkeypatch):
    from reposeer.pipelines.github import GitHubPipeline

    fake = FakeGitHub()
    service = RepositoryService(fake, storage_paths=StoragePaths(tmp_path))
    monkeypatch.setattr("reposeer.pipelines.github.RepositoryService", lambda: service)
    pipeline = GitHubPipeline()
    pipeline.run("https://github.com/Synthetic/Demo")
    assert fake.calls == [("Synthetic", "Demo")]
    fake.payload.pop("created_at")
    with pytest.raises(CollectionError):
        pipeline.run("Synthetic/Demo")


def test_provider_offset_conversion_has_no_historical_backfill():
    fake = FakeGitHub()
    request = validate_record(example("collection_request"))

    def clock():
        return datetime.fromisoformat("2024-02-29T12:00:00+00:00")

    result = GitHubRepositoryContractCollector(fake, clock=clock).collect(request)
    raw = result.records[0]
    normalized = GitHubRepositoryContractNormalizer().normalize(normalization(raw)).records[0]
    assert normalized.updated_at == "2024-02-01T00:00:00.000Z"
    assert (
        normalized.provenance.valid_at
        == normalized.provenance.retrieved_at
        == "2024-02-29T12:00:00.000Z"
    )
    assert raw.payload == fake.payload
    assert raw.raw_body_sha256 is None


@pytest.mark.parametrize("valid", [True, False])
def test_cli_runs_canonical_service_once_and_reports_real_outcome(tmp_path, monkeypatch, valid):
    from typer.testing import CliRunner

    from reposeer.cli.app import app

    fake = FakeGitHub()
    if not valid:
        fake.payload.pop("created_at")
    service = RepositoryService(fake, storage_paths=StoragePaths(tmp_path))
    monkeypatch.setattr("reposeer.pipelines.github.RepositoryService", lambda: service)
    result = CliRunner().invoke(app, ["collect", "Synthetic/Demo"])
    assert result.exit_code == (0 if valid else 1), result.output
    assert fake.calls == [("Synthetic", "Demo")]
    assert (tmp_path / "processed/repositories/repositories.parquet").exists() is valid
    if not valid:
        assert "completed successfully" not in result.output


def test_failed_collection_creates_no_raw_or_processed_record(tmp_path):
    fake = FakeGitHub()
    fake.payload["id"] = "900000001"
    service = RepositoryService(fake, storage_paths=StoragePaths(tmp_path))
    with pytest.raises(CollectionError):
        service.collect_and_store("Synthetic", "Demo")
    assert not list(tmp_path.rglob("*.jsonl"))
    assert not list(tmp_path.rglob("*.parquet"))


@pytest.mark.parametrize("identifier", ["name", "url"])
def test_repo_subject_does_not_claim_unverified_package_mapping(identifier):
    data = example("collection_request")
    data["subjects"][0].update(repo_id=None, package_id="pypi:unverified")
    if identifier == "url":
        data["subjects"][0].update(repo_full_name=None, url="https://github.com/Synthetic/Demo")
    request = validate_record(data)
    result = GitHubRepositoryContractCollector(FakeGitHub()).collect(request)
    validate_collection_pair(request, result)
    assert result.status == "complete"
    assert result.records[0].repo_id == "github:repository:900000001"
    assert result.records[0].package_id is None
