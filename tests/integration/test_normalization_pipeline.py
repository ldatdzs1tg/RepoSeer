"""Offline collector-to-T-018 handoff through real upstream adapters and persisted JSONL."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from reposeer.collection.http.client import HTTPClient
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.collectors.github.activity import GitHubActivityContractCollector
from reposeer.contracts import (
    CollectionRequest,
    CollectionSubject,
    validate_normalization_pair,
    validate_record,
)
from reposeer.contracts.adapters import (
    GitHubRepositoryContractCollector,
    GitHubRepositoryContractNormalizer,
)
from reposeer.pipelines.external import ExternalPipeline
from reposeer.pipelines.external_crawl import ExternalCrawlPipeline
from reposeer.pipelines.normalization import NormalizationPipeline, NormalizationPipelineResult
from reposeer.processing.relevance import VerifiedTargetContext
from reposeer.schemas.external.candidate import ExternalCandidate
from reposeer.storage.paths import StoragePaths
from reposeer.storage.raw.jsonl import JsonlStorage

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/external_crawler"
NOW = datetime(2026, 10, 18, 10, tzinfo=UTC)


@pytest.mark.parametrize("with_projection_state", [False, True])
def test_discovery_capture_article_grouping_relevance_and_jsonl_replay(tmp_path, with_projection_state):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "hn.algolia.com":
            return httpx.Response(200, json={"hits": [{"objectID": "42", "title": "Discovered title",
                                   "url": "https://blog.example/article", "created_at": "2024-03-01T00:00:00Z"}], "nbPages": 1})
        html = (FIXTURES / "article.html").read_text(encoding="utf-8")
        html = html.replace("<article>", "<article><p>Demo is a Python library with documented installation instructions.</p>")
        return httpx.Response(200, text=html,
                              headers={"content-type": "text/html; charset=utf-8"})

    storage = StoragePaths(tmp_path)
    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        discovery = ExternalPipeline(http_client=http, storage_paths=storage, clock=lambda: NOW).discover(
            "Synthetic/Demo", sources=["hackernews"], max_pages=1)
        assert discovery.collection_result.records
        candidate = discovery.collection_result.records[0]
        before = candidate.model_dump_json()
        with ExternalCrawlPipeline(http_client=http, storage_paths=storage,
                                   policy=URLPolicy(lambda host: ["93.184.216.34"]), clock=lambda: NOW) as crawler:
            states = [Path(discovery.discovery_manifest["candidate_projection_path"])] if with_projection_state else []
            first = crawler.crawl([candidate], discovery_state_paths=states)
            second = crawler.crawl([candidate], discovery_state_paths=states)
    raw_path = first.output_path
    before_file = raw_path.read_bytes()
    target = VerifiedTargetContext(package_id="pypi:demo", verified_aliases=["Demo"],
                                   mapping_evidence_refs=["fixture:verified-pypi-identity"])
    result = NormalizationPipeline().process_jsonl([raw_path], targets=[target])
    assert calls.count("https://blog.example/article") == 2
    assert any(url.startswith("https://hn.algolia.com/") for url in calls)
    serialized = json.loads(result.model_dump_json())
    assert NormalizationPipelineResult.model_validate(serialized).model_dump(mode="json") == serialized
    assert len(result.canonical_articles) == 2
    assert len(result.canonical_normalization_results) == 2  # distinct real collection runs
    assert len(result.deduplicated_document_view) == 1
    group = result.deduplicated_document_view[0]
    assert len(group.raw_record_ids) == 2 and len(group.distinct_content_hashes) == 1
    assert all(article.package_ids == article.repo_ids == [] for article in result.canonical_articles)
    assert all(ref.capture_source == "web" and ref.discovery_lineage_complete is with_projection_state
               for ref in result.source_lineage_references)
    for ref in result.source_lineage_references:
        assert ref.metadata["candidate_raw_record_id"] == candidate.raw_record_id
        assert ref.metadata["discovery_provider"] == "hackernews"
        assert ref.metadata["discovery"]["run_id"] == candidate.run_id
        assert ref.metadata["discovery_references"]
    assert all(assessment.context_level == "package_specific" for assessment in result.relevance_assessments)
    assert raw_path.read_bytes() == before_file and candidate.model_dump_json() == before
    assert second.output_path == first.output_path


@pytest.mark.parametrize("source,url,native_id", [
    ("hackernews", "https://news.ycombinator.com/item?id=42", "hn:42"),
    ("devto", "https://dev.to/fixture/python-release", "devto:42"),
    ("stackexchange", "https://stackoverflow.com/questions/42/fixture", "se:42"),
])
def test_native_api_fetch_url_is_not_article_identity(tmp_path, source, url, native_id):
    body = (FIXTURES / f"{source}.json").read_bytes()
    with HTTPClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, content=body, headers={"content-type": "application/json"}))) as http:
        capture = ExternalCrawlPipeline(http_client=http, storage_paths=StoragePaths(tmp_path),
                                        policy=URLPolicy(lambda host: ["93.184.216.34"]), clock=lambda: NOW).crawl([
            ExternalCandidate(title="Discovery", url=url, source=source, source_item_id=native_id)])
    raw = capture.collection_result.records[0]
    assert raw.payload["fetched_url"] != url
    article = NormalizationPipeline().process([raw]).canonical_articles[0]
    assert article.canonical_url == url and article.original_url == url
    assert article.provenance.source_url == raw.source_url


def test_repository_and_five_activity_groups_keep_observation_and_event_counts(tmp_path):
    payload = json.loads((ROOT / "examples/contracts/github_raw_record.json").read_text(encoding="utf-8"))["payload"]
    collection_request = validate_record(json.loads((ROOT / "examples/contracts/collection_request.json").read_text(encoding="utf-8")))
    collected = GitHubRepositoryContractCollector(lambda owner, repo: payload, clock=lambda: NOW).collect(collection_request)
    raw_repo = collected.records[0]
    normal_request_data = json.loads((ROOT / "examples/contracts/normalization_request.json").read_text(encoding="utf-8"))
    normal_request_data.update(run_id=raw_repo.run_id, records=[raw_repo.model_dump(mode="json")])
    normal_request = validate_record(normal_request_data)
    repository = GitHubRepositoryContractNormalizer().normalize(normal_request).records[0]
    bodies = {
        "commits": {"sha": "a"*40, "commit": {"committer": {"date": "2024-01-01T00:00:00Z"}}},
        "issues": {"id": 1000, "number": 1, "title": "Issue", "state": "open", "created_at": "2024-01-01T00:00:00Z"},
        "pull_requests": {"id": 2000, "number": 1, "title": "PR", "state": "open", "created_at": "2024-01-01T00:00:00Z"},
        "contributors": {"id": 42, "login": "fixture", "contributions": 5, "type": "User"},
        "releases": {"id": 3000, "tag_name": "v1", "draft": False, "prerelease": False},
    }
    raw_activity = []
    for group, body in bodies.items():
        client = SimpleNamespace(base_url="https://api.github.com", **{
            f"list_{group}": lambda owner, repo, body=body, **kwargs: [body],
        })

        request = CollectionRequest(schema_version="0.1.0", record_type="collection_request",
                                    request_id=str(uuid4()), run_id=str(uuid4()), collector_name="github_activity",
                                    source="github_rest", subjects=[CollectionSubject(repo_id=repository.identity.repo_id,
                                    repo_full_name=repository.identity.repo_full_name, package_id=None, url=None)],
                                    record_kinds=["repository_activity"], time_range=None, cutoff_at=None,
                                    cursor=None, max_pages=1, max_records=10)
        collection = GitHubActivityContractCollector(group, repository, client, clock=lambda: NOW).collect(request)
        assert collection.records, collection.errors
        raw_activity.extend(collection.records)
    inputs = [raw_repo, *raw_activity]
    repeated_commit = raw_activity[0].model_copy(deep=True)
    repeated_commit.raw_record_id = "raw:" + str(uuid4())
    repeated_commit.run_id = str(uuid4())
    inputs.append(repeated_commit)
    raw_path = tmp_path / "observations.jsonl"
    JsonlStorage().append_contract_records_durable(raw_path, inputs)
    before = raw_path.read_bytes()
    result = NormalizationPipeline().process_jsonl([raw_path])
    assert len(result.canonical_repositories) == 1 and len(result.canonical_activities) == 4
    assert len(result.internal_activity_entities) == 6
    assert len(result.deduplicated_activity_view) == 5
    assert result.processing_summary.internal_only_observation_count == 2
    assert result.processing_errors == []
    assert result.processing_summary.rejected_observation_count == 0
    assert len(next(group for group in result.deduplicated_activity_view if ":commit:" in group.logical_id).raw_record_ids) == 2
    assert {activity.source_object_id for activity in result.canonical_activities} == {"a"*40, "1000", "2000"}
    assert all(activity.activity_kind not in {"issue_reopened", "pr_reopened"} for activity in result.canonical_activities)
    for canonical in result.canonical_normalization_results:
        from reposeer.contracts import NormalizationRequest
        request = NormalizationRequest(schema_version="0.1.0", record_type="normalization_request", request_id=canonical.request_id,
                                        run_id=canonical.run_id, records=[raw for raw in inputs if raw.run_id == canonical.run_id
                                                                       and raw.source_record_type == ("github_repository" if canonical.records and canonical.records[0].record_type == "github_repository" else "repository_activity")])
        validate_normalization_pair(request, canonical)
    assert raw_path.read_bytes() == before


def test_subset_missing_files_and_invalid_raw_accounting(tmp_path):
    result = NormalizationPipeline().process([{"record_type": "raw_record"}])
    assert result.processing_summary.rejected_observation_count == 1
    assert result.processing_errors[0].message == "invalid_raw_envelope"
    assert NormalizationPipeline().process().processing_summary.input_record_count == 0
    with pytest.raises(FileNotFoundError):
        NormalizationPipeline().process_jsonl([tmp_path / "missing.jsonl"])
