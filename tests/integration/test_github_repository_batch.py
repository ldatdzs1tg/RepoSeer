"""Offline T-013 batch collection, screening, storage and CLI regressions."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import polars as pl
import pytest
from typer.testing import CliRunner

from reposeer.clients.github import GitHubClient
from reposeer.collection import HTTPClient
from reposeer.contracts import GitHubRepository, NormalizationRequest, validate_normalization_pair
from reposeer.contracts.adapters import GitHubRepositoryContractNormalizer
from reposeer.exceptions import (
    CollectionError,
    HTTPClientError,
    RateLimitExceededError,
    StorageError,
)
from reposeer.services.repository_service import RepositoryService, parse_repository_target
from reposeer.storage.paths import StoragePaths

ROOT = Path(__file__).resolve().parents[2]


def native(slug, numeric_id, **changes):
    payload = json.loads(
        (ROOT / "examples/contracts/github_raw_record.json").read_text(encoding="utf-8")
    )["payload"]
    payload.update(id=numeric_id, full_name=slug, **changes)
    return payload


class FakeGitHub:
    def __init__(self):
        self.payloads = {
            "synthetic/alpha": native("Synthetic/Alpha", 900000001),
            "synthetic/beta": native("Synthetic/Beta", 900000002),
            "synthetic/gamma": native("Synthetic/Gamma", 900000003),
        }
        self.calls = []
        self.close_count = 0

    def get_repository(self, owner, repo):
        self.calls.append((owner, repo))
        value = self.payloads[f"{owner}/{repo}".lower()]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)

    def close(self):
        self.close_count += 1


@pytest.fixture
def batch_service(tmp_path):
    fake = FakeGitHub()
    return RepositoryService(fake, storage_paths=StoragePaths(tmp_path)), fake


def raws(service):
    return service.raw_storage.read_contract_records(
        service.paths.raw_path("github", "repositories")
    )


def frame(service):
    return pl.read_parquet(service.paths.processed_path("repositories"))


@pytest.mark.parametrize(
    "target,slug",
    [
        (" Synthetic/Alpha ", "Synthetic/Alpha"),
        ("https://github.com/Synthetic/Beta", "Synthetic/Beta"),
        ("HTTPS://GITHUB.COM/SYNTHETIC/Gamma/", "SYNTHETIC/Gamma"),
    ],
)
def test_parse_repository_target_preserves_supplied_case(target, slug):
    assert parse_repository_target(target) == slug


INVALID_TARGETS = [
    "https://example.com/owner/repo",
    "http://github.com/owner/repo",
    "https://github.com/owner/repo/issues",
    "https://github.com/owner/repo?tab=readme",
    "https://github.com/owner/repo?",
    "https://github.com/owner/repo#readme",
    "https://github.com/owner/repo#",
    "https://github.com/owner/repo.git",
    "https://user:synthetic-secret@github.com/owner/repo",
    "https://github.com:443/owner/repo",
    "https://github.com:/owner/repo",
    "https://github.com/owner/repo//",
    "https://github.com//owner/repo",
    "https://github.com/owner/re po",
    "owner",
    "owner/repo/extra",
    "owner/repo/",
    "owner/.",
    "owner/..",
    "github:repository:123",
    "",
    None,
    123,
]


@pytest.mark.parametrize("target", INVALID_TARGETS)
def test_invalid_target_never_fetches_or_leaks_input(batch_service, target, caplog):
    service, fake = batch_service
    with pytest.raises(ValueError):
        parse_repository_target(target)
    result = service.collect_many([target])
    assert result.status == "failed"
    assert result.requested_count == result.failed_count == 1
    assert result.raw_stored_count == result.processed_stored_count == 0
    assert result.items[0].error_code == "invalid_target"
    assert result.items[0].repo_id is None
    assert fake.calls == []
    assert "synthetic-secret" not in repr(result) + caplog.text


@pytest.mark.parametrize("targets", [[], ["Synthetic/Alpha"] * 1001, "Synthetic/Alpha"])
def test_batch_size_is_checked_before_fetch(batch_service, targets):
    service, fake = batch_service
    with pytest.raises(ValueError, match="1 to 1,000 targets"):
        service.collect_many(targets)
    assert not fake.calls


def test_three_repos_share_run_and_write_parquet_once(batch_service, monkeypatch):
    service, fake = batch_service
    requests, reads, writes = [], [], []
    collect = service.collector.collect
    read = service.parquet_storage.read
    write = service.parquet_storage.write

    def capture_request(request):
        requests.append(request)
        return collect(request)

    def capture_read(path):
        reads.append(path)
        return read(path)

    def capture_write(path, data):
        writes.append(path)
        write(path, data)

    monkeypatch.setattr(service.collector, "collect", capture_request)
    monkeypatch.setattr(service.parquet_storage, "read", capture_read)
    monkeypatch.setattr(service.parquet_storage, "write", capture_write)
    service.collector.clock = lambda: datetime.fromisoformat("2024-02-29T12:00:00+00:00")
    result = service.collect_many(
        [
            "Synthetic/Alpha",
            "https://github.com/Synthetic/Beta",
            "https://github.com/SYNTHETIC/Gamma/",
        ]
    )
    assert fake.calls == [("Synthetic", "Alpha"), ("Synthetic", "Beta"), ("SYNTHETIC", "Gamma")]
    assert result.status == "complete"
    assert result.requested_count == result.raw_stored_count == result.processed_stored_count == 3
    assert result.excluded_count == result.failed_count == result.duplicate_count == 0
    assert all(item.status == "stored" and not item.screening_reasons for item in result.items)
    assert all(item.error_code is None and item.error_message is None for item in result.items)
    assert UUID(result.run_id).version == 4
    assert len(reads) == len(writes) == 1
    assert len({request.request_id for request in requests}) == 3
    for request in requests:
        assert UUID(request.request_id).version == 4
        assert request.run_id == result.run_id
        assert request.record_kinds == ["github_repository"]
        assert request.source == "github_rest"
        assert request.collector_name == "github_repository_contract"
        assert request.time_range is request.cutoff_at is request.cursor is None
        assert request.max_pages == request.max_records == 1
        assert len(request.subjects) == 1
        assert (
            request.subjects[0].repo_id
            is request.subjects[0].package_id
            is request.subjects[0].url
            is None
        )
    raw_records = raws(service)
    assert len(raw_records) == len({raw.raw_record_id for raw in raw_records}) == 3
    for raw in raw_records:
        assert UUID(raw.raw_record_id.removeprefix("raw:")).version == 4
        assert raw.run_id == result.run_id
        assert raw.schema_version == "0.1.0"
        assert raw.source == "github_rest" and raw.source_record_type == "github_repository"
        assert raw.source_record_id == str(raw.payload["id"])
        assert raw.repo_id == "github:repository:" + raw.source_record_id
        assert raw.payload == fake.payloads[raw.payload["full_name"].lower()]
        assert raw.event_at is raw.available_at is raw.package_id is None
        assert raw.raw_body_sha256 is raw.http_status is None
        assert raw.retrieved_at == "2024-02-29T12:00:00.000Z"
        request = NormalizationRequest(
            schema_version="0.1.0",
            record_type="normalization_request",
            request_id=str(uuid4()),
            run_id=raw.run_id,
            records=[raw],
        )
        normalized = GitHubRepositoryContractNormalizer().normalize(request)
        validate_normalization_pair(request, normalized)
        assert normalized.records[0].provenance.raw_record_ids == [raw.raw_record_id]
        assert normalized.records[0].provenance.retrieved_at == raw.retrieved_at
        assert normalized.records[0].provenance.valid_at == raw.retrieved_at
    projected = frame(service)
    assert projected.height == 3
    strings = [
        "schema_version",
        "repo_id",
        "github_repo_id",
        "repo_full_name",
        "repo_url",
        "retrieved_at",
        "valid_at",
        "available_at",
        "temporal_basis",
        "source",
        "source_url",
        "raw_record_id",
    ]
    expected_schema = {name: pl.String for name in strings}
    expected_schema.update({name: pl.Boolean for name in ("is_archived", "is_fork", "is_template")})
    expected_schema.update(
        {
            name: pl.Int64
            for name in ("stargazers_count", "forks_count", "open_issues_and_prs_count")
        }
    )
    assert projected.schema == expected_schema
    assert projected["open_issues_and_prs_count"].to_list() == [2, 2, 2]
    assert "open_issues_count" not in projected.columns
    with pytest.raises(FrozenInstanceError):
        result.status = "failed"
    with pytest.raises(FrozenInstanceError):
        result.items[0].status = "failed"


@pytest.mark.parametrize("first_fails", [False, True])
def test_equivalent_slug_url_and_case_only_fetch_once(batch_service, first_fails):
    service, fake = batch_service
    if first_fails:
        fake.payloads["synthetic/alpha"] = HTTPClientError("secret", status_code=404)
    result = service.collect_many(
        ["Synthetic/Alpha", "SYNTHETIC/alpha", "https://github.com/synthetic/ALPHA/"]
    )
    assert fake.calls == [("Synthetic", "Alpha")]
    assert result.duplicate_count == 2
    assert [item.status for item in result.items[1:]] == ["duplicate", "duplicate"]
    assert result.raw_stored_count == result.processed_stored_count == (0 if first_fails else 1)
    assert result.status == ("failed" if first_fails else "complete")


def test_invalid_entry_does_not_block_valid_target(batch_service):
    service, fake = batch_service
    result = service.collect_many(["invalid", "Synthetic/Beta"])
    assert result.status == "partial"
    assert result.failed_count == result.raw_stored_count == result.processed_stored_count == 1
    assert [item.status for item in result.items] == ["failed", "stored"]
    assert fake.calls == [("Synthetic", "Beta")]


@pytest.mark.parametrize("alias_conflict", [False, True])
def test_duplicate_numeric_id_requires_valid_native_name(batch_service, alias_conflict):
    service, fake = batch_service
    fake.payloads["synthetic/beta"]["id"] = 900000001
    if alias_conflict:
        fake.payloads["synthetic/beta"]["full_name"] = "Synthetic/Alpha"
    result = service.collect_many(["Synthetic/Alpha", "Synthetic/Beta"])
    assert len(fake.calls) == 2
    assert result.raw_stored_count == result.processed_stored_count == 1
    assert result.items[1].status == ("failed" if alias_conflict else "duplicate")
    assert result.items[1].error_code == ("validation_error" if alias_conflict else None)
    assert result.status == ("partial" if alias_conflict else "complete")
    assert len(raws(service)) == frame(service).height == 1


@pytest.mark.parametrize(
    "error,code",
    [
        (HTTPClientError("synthetic-secret", status_code=404), "not_found"),
        (HTTPClientError("synthetic-secret", status_code=401), "http_error"),
        (HTTPClientError("synthetic-secret", status_code=403), "http_error"),
        (RateLimitExceededError("synthetic-secret", status_code=403), "rate_limited"),
        (RateLimitExceededError("synthetic-secret"), "rate_limited"),
        (HTTPClientError("synthetic-secret", status_code=503), "http_error"),
        (HTTPClientError("synthetic-secret"), "network_error"),
        (HTTPClientError("synthetic-secret", status_code=200), "parse_error"),
    ],
)
def test_batch_classifies_errors_and_continues(batch_service, error, code, caplog):
    service, fake = batch_service
    fake.payloads["synthetic/beta"] = error
    result = service.collect_many(["Synthetic/Alpha", "Synthetic/Beta", "Synthetic/Gamma"])
    assert result.status == "partial"
    assert result.failed_count == 1
    assert result.raw_stored_count == result.processed_stored_count == 2
    assert result.items[1].error_code == code
    assert len(fake.calls) == 3
    assert len(raws(service)) == frame(service).height == 2
    assert "synthetic-secret" not in repr(result) + caplog.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", None),
        ("id", True),
        ("id", "123"),
        ("full_name", None),
        ("full_name", "Synthetic/Other"),
    ],
)
def test_invalid_identity_does_not_persist_or_block_other_repos(batch_service, field, value):
    service, fake = batch_service
    fake.payloads["synthetic/alpha"][field] = value
    result = service.collect_many(["Synthetic/Alpha", "Synthetic/Beta"])
    assert result.status == "partial"
    assert result.items[0].error_code == "validation_error"
    assert result.items[0].repo_id is None
    assert result.raw_stored_count == result.processed_stored_count == 1
    assert raws(service)[0].payload["full_name"] == "Synthetic/Beta"


def test_all_collection_failures_report_failed(batch_service):
    service, fake = batch_service
    fake.payloads["synthetic/alpha"] = HTTPClientError("secret", status_code=404)
    fake.payloads["synthetic/beta"] = HTTPClientError("secret", status_code=503)
    result = service.collect_many(["Synthetic/Alpha", "Synthetic/Beta"])
    assert result.status == "failed"
    assert result.failed_count == result.requested_count == 2
    assert result.raw_stored_count == result.processed_stored_count == 0
    assert not list(service.paths.base_dir.rglob("*.jsonl"))
    assert not list(service.paths.base_dir.rglob("*.parquet"))


def test_rerun_appends_new_observations_and_retains_history(batch_service):
    service, fake = batch_service
    first = service.collect_many(["Synthetic/Alpha"])
    raw_file = service.paths.raw_path("github", "repositories")
    first_bytes = raw_file.read_bytes()
    second = service.collect_many(["Synthetic/Alpha"])
    assert first.run_id != second.run_id
    assert first.status == second.status == "complete"
    assert first.duplicate_count == second.duplicate_count == 0
    assert len(fake.calls) == len(raws(service)) == frame(service).height == 2
    assert raw_file.read_bytes().startswith(first_bytes)
    assert len({raw.raw_record_id for raw in raws(service)}) == 2


@pytest.mark.parametrize(
    "changes,reasons,raw_count",
    [
        ({"fork": True}, ("fork_repository",), 1),
        ({"is_template": True}, ("template_repository",), 1),
        ({"mirror_url": "https://example.test/mirror"}, ("mirror_repository",), 1),
        (
            {"fork": True, "is_template": True, "mirror_url": "https://example.test/mirror"},
            ("fork_repository", "template_repository", "mirror_repository"),
            1,
        ),
        ({"private": True}, ("private_repository",), 0),
        ({"visibility": "private"}, ("private_repository",), 0),
        ({"visibility": "internal"}, ("internal_repository",), 0),
    ],
)
def test_scope_exclusions_have_accurate_persistence(
    batch_service, changes, reasons, raw_count, caplog
):
    service, fake = batch_service
    fake.payloads["synthetic/alpha"].update(changes)
    if not raw_count:
        fake.payloads["synthetic/alpha"]["description"] = "synthetic-private-details"
    result = service.collect_many(["Synthetic/Alpha"])
    assert result.status == "complete"
    assert result.excluded_count == 1 and result.failed_count == 0
    assert result.raw_stored_count == raw_count and result.processed_stored_count == 0
    assert result.items[0].status == "excluded"
    assert result.items[0].screening_reasons == reasons
    assert result.items[0].repo_id == "github:repository:900000001"
    assert result.items[0].error_code is result.items[0].error_message is None
    assert len(raws(service)) == raw_count
    assert not list(service.paths.base_dir.rglob("*.parquet"))
    assert "synthetic-private-details" not in repr(result) + caplog.text


def test_excluded_plus_failure_reports_partial(batch_service):
    service, fake = batch_service
    fake.payloads["synthetic/alpha"]["private"] = True
    fake.payloads["synthetic/beta"] = HTTPClientError("secret", status_code=404)
    result = service.collect_many(["Synthetic/Alpha", "Synthetic/Beta"])
    assert result.status == "partial"
    assert result.excluded_count == result.failed_count == 1
    assert result.raw_stored_count == result.processed_stored_count == 0


@pytest.mark.parametrize("language", ["Python", "Rust", "C++", None])
@pytest.mark.parametrize("archived", [False, True])
def test_archived_and_nonpython_language_are_not_excluded(batch_service, language, archived):
    service, fake = batch_service
    fake.payloads["synthetic/alpha"].update(language=language, archived=archived)
    result = service.collect_many(["Synthetic/Alpha"])
    assert result.items[0].status == "stored"
    assert result.items[0].screening_reasons == ()
    assert result.raw_stored_count == result.processed_stored_count == 1
    assert frame(service)["is_archived"][0] is archived
    assert raws(service)[0].payload == fake.payloads["synthetic/alpha"]


@pytest.mark.parametrize("missing", [False, True])
def test_missing_counts_stay_nullable_int64(batch_service, missing):
    service, fake = batch_service
    for name in ("stargazers_count", "forks_count", "open_issues_count"):
        if missing:
            fake.payloads["synthetic/alpha"].pop(name)
        else:
            fake.payloads["synthetic/alpha"][name] = None
    result = service.collect_many(["Synthetic/Alpha", "Synthetic/Beta"])
    assert result.processed_stored_count == 2
    projected = frame(service)
    for name in ("stargazers_count", "forks_count", "open_issues_and_prs_count"):
        assert projected[name][0] is None
        assert projected.schema[name] == pl.Int64
    request = NormalizationRequest(
        schema_version="0.1.0",
        record_type="normalization_request",
        request_id=str(uuid4()),
        run_id=result.run_id,
        records=[raws(service)[0]],
    )
    normalized = GitHubRepositoryContractNormalizer().normalize(request)
    missing_fields = {item.field_path for item in normalized.records[0].provenance.missing_fields}
    assert {"stargazers_count", "forks_count", "open_issues_and_prs_count"} <= missing_fields


@pytest.mark.parametrize(
    "field,value", [("fork", None), ("created_at", "invalid"), ("stargazers_count", "5")]
)
def test_normalization_failure_keeps_raw_and_continues(batch_service, field, value):
    service, fake = batch_service
    fake.payloads["synthetic/alpha"][field] = value
    result = service.collect_many(["Synthetic/Alpha", "Synthetic/Beta"])
    assert result.status == "partial"
    assert result.raw_stored_count == 2 and result.processed_stored_count == 1
    assert result.items[0].error_code == "normalization_error"
    assert result.items[0].repo_id == "github:repository:900000001"
    assert len(raws(service)) == 2 and frame(service).height == 1


def test_batch_rejects_normalizer_retrieval_drift(batch_service, monkeypatch):
    service, _ = batch_service
    normalize = service.normalizer.normalize

    def drift(request):
        result = normalize(request)
        record = result.records[0]
        provenance = record.provenance.model_copy(
            update={
                "retrieved_at": "2099-01-01T00:00:00.000Z",
                "valid_at": "2099-01-01T00:00:00.000Z",
            }
        )
        return result.model_copy(
            update={"records": [record.model_copy(update={"provenance": provenance})]}
        )

    monkeypatch.setattr(service.normalizer, "normalize", drift)
    result = service.collect_many(["Synthetic/Alpha"])
    assert result.status == "failed"
    assert result.items[0].error_code == "normalization_error"
    assert result.raw_stored_count == 1 and result.processed_stored_count == 0
    assert len(raws(service)) == 1
    assert not list(service.paths.base_dir.rglob("*.parquet"))


def test_raw_write_failure_is_not_counted_as_success(batch_service, monkeypatch):
    service, _ = batch_service
    append = service.raw_storage.append_contract_record

    def fail_first(path, raw):
        if raw.payload["full_name"] == "Synthetic/Alpha":
            raise OSError("synthetic-secret")
        append(path, raw)

    monkeypatch.setattr(service.raw_storage, "append_contract_record", fail_first)
    result = service.collect_many(["Synthetic/Alpha", "Synthetic/Beta"])
    assert result.status == "partial"
    assert result.items[0].error_code == "storage_error"
    assert result.raw_stored_count == result.processed_stored_count == 1
    assert len(raws(service)) == frame(service).height == 1
    assert "synthetic-secret" not in repr(result)


@pytest.mark.parametrize("empty", [False, True])
def test_schema_mismatch_raises_without_rewriting_old_file(batch_service, empty):
    service, _ = batch_service
    path = service.paths.processed_path("repositories")
    old = (
        pl.DataFrame(schema={"repo_id": pl.String})
        if empty
        else pl.DataFrame({"repo_id": ["legacy"]})
    )
    old.write_parquet(path)
    previous = path.read_bytes()
    with pytest.raises(CollectionError, match="explicit migration"):
        service.collect_many(["Synthetic/Alpha"])
    assert path.read_bytes() == previous
    assert len(raws(service)) == 1


@pytest.mark.parametrize(
    "error", [OSError("secret"), StorageError("secret"), pl.exceptions.ComputeError("secret")]
)
def test_final_parquet_write_failure_raises_and_preserves_raw(batch_service, monkeypatch, error):
    service, _ = batch_service
    service.collect_many(["Synthetic/Alpha"])
    path = service.paths.processed_path("repositories")
    previous = path.read_bytes()

    def fail_write(path, data):
        raise error

    monkeypatch.setattr(service.parquet_storage, "write", fail_write)
    with pytest.raises(CollectionError, match="Unable to persist processed repository data"):
        service.collect_many(["Synthetic/Beta"])
    assert len(raws(service)) == 2
    assert path.read_bytes() == previous


@pytest.mark.parametrize(
    "changes", [{}, {"private": True}, {"visibility": "private"}, {"visibility": "internal"}]
)
def test_single_repository_remains_canonical_and_blocks_private(batch_service, changes):
    service, fake = batch_service
    fake.payloads["synthetic/alpha"].update(changes)
    if changes:
        with pytest.raises(CollectionError, match="public metadata scope"):
            service.collect_and_store("Synthetic", "Alpha")
        assert not list(service.paths.base_dir.rglob("*.jsonl"))
        assert not list(service.paths.base_dir.rglob("*.parquet"))
    else:
        record = service.collect_and_store("Synthetic", "Alpha")
        assert isinstance(record, GitHubRepository)
        assert record.identity.repo_full_name == "Synthetic/Alpha"
        assert len(raws(service)) == frame(service).height == 1


def test_batch_reuses_shared_http_and_leaves_injected_session_open(tmp_path):
    fake = FakeGitHub()
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "example.test":
            return httpx.Response(200)
        slug = request.url.path.removeprefix("/repos/").lower()
        return httpx.Response(200, json=fake.payloads[slug])

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        client = GitHubClient(token="synthetic-secret", http_client=http)
        service = RepositoryService(client, storage_paths=StoragePaths(tmp_path))
        result = service.collect_many(["Synthetic/Alpha", "Synthetic/Beta"])
        service.close()
        client.close()
        assert http.get("https://example.test/ping").status_code == 200
    assert result.status == "complete"
    assert len(requests) == 3
    assert "authorization" not in requests[-1].headers
    assert "synthetic-secret" not in service.paths.raw_path("github", "repositories").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize("injected", [False, True])
def test_service_closes_only_owned_client(tmp_path, monkeypatch, injected):
    fake = FakeGitHub()
    monkeypatch.setattr("reposeer.contracts.adapters.github_repository.GitHubClient", lambda: fake)
    service = RepositoryService(fake if injected else None, storage_paths=StoragePaths(tmp_path))
    service.collect_many(["Synthetic/Alpha"])
    service.close()
    assert fake.close_count == (0 if injected else 1)


def test_unexpected_programming_error_propagates(batch_service):
    service, fake = batch_service
    fake.payloads["synthetic/alpha"] = RuntimeError("programming failure")
    with pytest.raises(RuntimeError, match="programming failure"):
        service.collect_many(["Synthetic/Alpha"])


def test_cli_batch_reads_file_and_prints_complete_summary(batch_service, tmp_path, monkeypatch):
    from reposeer.cli.app import app

    service, fake = batch_service
    monkeypatch.setattr("reposeer.cli.collect.RepositoryService", lambda: service)
    path = tmp_path / "repositories.txt"
    path.write_text(
        " # comment\n\nSynthetic/Alpha\nhttps://github.com/Synthetic/Beta/\n", encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["collect", "--repos-file", str(path)])
    assert result.exit_code == 0, result.output
    for line in (
        "Requested: 2",
        "Raw stored: 2",
        "Processed stored: 2",
        "Excluded: 0",
        "Duplicates: 0",
        "Failed: 0",
        "Status: complete",
    ):
        assert line in result.output
    assert fake.calls == [("Synthetic", "Alpha"), ("Synthetic", "Beta")]
    assert len(raws(service)) == frame(service).height == 2


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_cli_reports_batch_failures_and_nonzero_exit(batch_service, tmp_path, monkeypatch, status):
    from reposeer.cli.app import app

    service, fake = batch_service
    fake.payloads["synthetic/alpha"] = HTTPClientError("synthetic-secret", status_code=404)
    monkeypatch.setattr("reposeer.cli.collect.RepositoryService", lambda: service)
    path = tmp_path / "repositories.txt"
    path.write_text(
        "Synthetic/Alpha\n" + ("Synthetic/Beta\n" if status == "partial" else ""), encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["collect", "--repos-file", str(path), "--source", "github"])
    assert result.exit_code == 1
    assert f"Status: {status}" in result.output
    assert "Synthetic/Alpha: not_found" in result.output
    assert "synthetic-secret" not in result.output


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "empty",
        "directory",
        "bad_utf8",
        "too_many",
        "both",
        "neither",
        "pypi",
        "external",
    ],
)
def test_cli_invalid_inputs_fail_before_service_creation(tmp_path, monkeypatch, case):
    from reposeer.cli.app import app

    def forbidden_service():
        raise AssertionError("Invalid CLI input must not create a client")

    monkeypatch.setattr("reposeer.cli.collect.RepositoryService", forbidden_service)
    path = tmp_path / "repositories.txt"
    args = ["collect", "--repos-file", str(path)]
    if case == "empty":
        path.write_text("\n # comment\n", encoding="utf-8")
    elif case == "directory":
        path.mkdir()
    elif case == "bad_utf8":
        path.write_bytes(b"\xff")
    elif case == "too_many":
        path.write_text("Synthetic/Alpha\n" * 1001, encoding="utf-8")
    elif case == "both":
        path.write_text("Synthetic/Alpha\n", encoding="utf-8")
        args.append("Synthetic/Alpha")
    elif case == "neither":
        args = ["collect"]
    elif case in {"pypi", "external"}:
        args += ["--source", case]
    result = CliRunner().invoke(app, args, catch_exceptions=False)
    assert result.exit_code == 1, result.output


@pytest.mark.parametrize("target", ["Synthetic/Alpha", "https://github.com/Synthetic/Alpha"])
def test_cli_single_repository_compatibility(batch_service, monkeypatch, target):
    from reposeer.cli.app import app

    service, fake = batch_service
    monkeypatch.setattr("reposeer.pipelines.github.RepositoryService", lambda: service)
    result = CliRunner().invoke(app, ["collect", target])
    assert result.exit_code == 0, result.output
    assert "completed successfully" in result.output
    assert fake.calls == [("Synthetic", "Alpha")]
    assert len(raws(service)) == frame(service).height == 1


def test_cli_persistence_failure_never_reports_complete(batch_service, tmp_path, monkeypatch):
    from reposeer.cli.app import app

    service, _ = batch_service
    monkeypatch.setattr("reposeer.cli.collect.RepositoryService", lambda: service)

    def fail_write(path, data):
        raise OSError("synthetic-secret")

    monkeypatch.setattr(service.parquet_storage, "write", fail_write)
    path = tmp_path / "repositories.txt"
    path.write_text("Synthetic/Alpha\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["collect", "--repos-file", str(path)])
    assert result.exit_code == 1
    assert "Status: complete" not in result.output
    assert "synthetic-secret" not in result.output
    assert len(raws(service)) == 1


def test_cli_does_not_parse_inline_comments(batch_service, tmp_path, monkeypatch):
    from reposeer.cli.app import app

    service, fake = batch_service
    monkeypatch.setattr("reposeer.cli.collect.RepositoryService", lambda: service)
    path = tmp_path / "repositories.txt"
    path.write_text("Synthetic/Alpha # comment\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["collect", "--repos-file", str(path)])
    assert result.exit_code == 1
    assert "invalid_target" in result.output
    assert not fake.calls


def test_maximum_batch_size_accepts_duplicates(batch_service):
    service, fake = batch_service
    result = service.collect_many(["Synthetic/Alpha"] * 1000)
    assert result.status == "complete"
    assert result.requested_count == len(result.items) == 1000
    assert result.duplicate_count == 999
    assert result.raw_stored_count == result.processed_stored_count == 1
    assert fake.calls == [("Synthetic", "Alpha")]


def test_cli_exclusions_duplicates_and_service_cleanup(batch_service, tmp_path, monkeypatch):
    from reposeer.cli.app import app

    service, fake = batch_service
    fake.payloads["synthetic/alpha"]["private"] = True
    fake.payloads["synthetic/beta"]["fork"] = True
    monkeypatch.setattr("reposeer.cli.collect.RepositoryService", lambda: service)
    closed = []
    monkeypatch.setattr(service, "close", lambda: closed.append(True))
    path = tmp_path / "repositories.txt"
    path.write_text("Synthetic/Alpha\nSynthetic/Beta\nSYNTHETIC/Beta\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["collect", "--repos-file", str(path)])
    assert result.exit_code == 0, result.output
    for line in (
        "Excluded: 2",
        "Duplicates: 1",
        "Raw stored: 1",
        "Processed stored: 0",
        "Synthetic/Alpha: private_repository",
        "Synthetic/Beta: fork_repository",
    ):
        assert line in result.output
    assert closed == [True]
    assert len(fake.calls) == 2


@pytest.mark.parametrize("batch", [False, True])
def test_cli_credentials_are_never_echoed(batch_service, tmp_path, monkeypatch, batch):
    from reposeer.cli.app import app

    service, fake = batch_service
    monkeypatch.setattr("reposeer.cli.collect.RepositoryService", lambda: service)
    target = "https://user:synthetic-secret@github.com/Synthetic/Alpha"
    if batch:
        path = tmp_path / "repositories.txt"
        path.write_text(target + "\n", encoding="utf-8")
        args = ["collect", "--repos-file", str(path)]
    else:
        args = ["collect", target]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert "synthetic-secret" not in result.output
    assert not fake.calls
