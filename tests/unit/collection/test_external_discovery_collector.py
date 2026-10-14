"""Unit tests for ExternalDiscoveryCollector (T-016 Sub-Task 16.4)."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from reposeer.clients.external import (
    DiscoveryPageResult,
    DiscoveryProvider,
    RSSProvider,
)
from reposeer.collection import CollectionProgress, RunManager
from reposeer.collectors.external.discovery import ExternalDiscoveryCollector
from reposeer.contracts import (
    CollectionRequest,
    CollectionResult,
    CollectionSubject,
    RawRecord,
    RepoIdentity,
    TimeRange,
    validate_record,
)
from reposeer.contracts.identity import (
    canonical_github_url,
    repo_id,
)
from reposeer.exceptions import HTTPClientError
from reposeer.schemas.external.candidate import ExternalCandidate
from reposeer.storage.raw.jsonl import JsonlStorage
from reposeer.technology.models import SearchPlan, SearchTerm


class MockDiscoveryProvider(DiscoveryProvider):
    """Mock discovery provider for unit tests."""

    def __init__(
        self,
        provider_name: str = "mock_hn",
        *,
        supports_historical: bool = True,
        pages: list[DiscoveryPageResult] | None = None,
        error_to_raise: Exception | None = None,
    ):
        self.provider_name = provider_name
        self.supports_historical_window = supports_historical
        self.pages = pages or []
        self.error_to_raise = error_to_raise
        self.calls: list[dict[str, Any]] = []

    def search_page(
        self,
        query: str,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> DiscoveryPageResult:
        self.calls.append({
            "query": query,
            "window_start": window_start,
            "window_end": window_end,
            "cursor": cursor,
            "page_size": page_size,
        })
        if self.error_to_raise is not None:
            raise self.error_to_raise

        if cursor is not None and cursor.isdigit():
            page_idx = int(cursor)
        else:
            page_idx = len(self.calls) - 1

        if page_idx < len(self.pages):
            return self.pages[page_idx]
        return DiscoveryPageResult(candidates=[], page_number=page_idx + 1, has_more=False)

    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        return self.search_page(query, page_size=limit).candidates[:limit]


def _make_request(
    *,
    run_id: str | None = None,
    max_pages: int = 10,
    max_records: int = 100,
    cutoff_at: str | None = None,
    time_range: TimeRange | None = None,
    cursor: str | None = None,
) -> CollectionRequest:
    return CollectionRequest(
        schema_version="0.1.0",
        record_type="collection_request",
        request_id=str(uuid4()),
        run_id=run_id or str(uuid4()),
        collector_name="external_discovery",
        source="web_article",
        subjects=[
            CollectionSubject(
                repo_id=None,
                repo_full_name="pallets/flask",
                package_id="pypi:flask",
                url=None,
            )
        ],
        record_kinds=["article"],
        time_range=time_range,
        cutoff_at=cutoff_at,
        cursor=cursor,
        max_pages=max_pages,
        max_records=max_records,
    )


def _make_repo_identity() -> RepoIdentity:
    github_id = "12345"
    return RepoIdentity(
        repo_id=repo_id(github_id),
        github_repo_id=github_id,
        github_node_id=None,
        repo_full_name="pallets/flask",
        repo_url=canonical_github_url("pallets/flask"),
        known_urls=[canonical_github_url("pallets/flask")],
    )


def test_contract_compliance():
    """Verify that every emitted RawRecord and CollectionResult pass validate_record() with 0 schema errors."""
    c1 = ExternalCandidate(
        title="Flask 3.0 Released",
        url="https://palletsprojects.com/blog/flask-3",
        source="hackernews",
        source_item_id="hn:1001",
        publication_date=datetime(2023, 10, 1, 12, 0, 0, tzinfo=UTC),
        score=42.0,
        snippet="Official Flask 3 announcement",
    )
    c2 = ExternalCandidate(
        title="How to structure large Flask applications",
        url="https://blog.miguelgrinberg.com/post/flask-structure",
        source="hackernews",
        source_item_id="hn:1002",
        publication_date=datetime(2023, 11, 1, 10, 0, 0, tzinfo=UTC),
        score=15.0,
        snippet="Design patterns for Flask",
    )
    mock_provider = MockDiscoveryProvider(
        provider_name="hackernews",
        supports_historical=True,
        pages=[DiscoveryPageResult(candidates=[c1, c2], page_number=1, has_more=False)],
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        clock=lambda: datetime(2024, 2, 1, 0, 0, 0, tzinfo=UTC),
    )

    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        repo_full_name="pallets/flask",
        terms=[
            SearchTerm(
                query="pallets/flask release",
                intent="announcement",
                platform="hackernews",
                priority=1,
                keywords=["flask", "release"],
            )
        ],
    )
    repo_id_obj = _make_repo_identity()

    result, manifest = collector.collect_discovery(
        request, plan, repo_identity=repo_id_obj, package_id="pypi:flask"
    )

    # 1. CollectionResult validation
    validated_res = validate_record(result)
    assert isinstance(validated_res, CollectionResult)
    assert result.status == "complete"
    assert result.metadata.pagination_complete is True
    assert result.metadata.pages_fetched == 1
    assert result.metadata.records_emitted == 2
    assert result.metadata.records_rejected == 0
    assert result.metadata.coverage[0].status == "complete"
    assert result.metadata.coverage[0].records_observed == 2
    assert result.metadata.coverage[0].missing_reason is None

    # 2. RawRecord validation
    assert len(result.records) == 2
    for raw in result.records:
        validated_raw = validate_record(raw)
        assert isinstance(validated_raw, RawRecord)
        assert raw.schema_version == "0.1.0"
        assert raw.record_type == "raw_record"
        assert raw.source == "web_article"
        assert raw.source_record_type == "article"
        assert raw.repo_id == repo_id_obj.repo_id
        assert raw.package_id == "pypi:flask"
        assert raw.available_at is None
        assert raw.http_status == 200
        assert raw.raw_body_sha256 is not None
        assert raw.retrieved_at == "2024-02-01T00:00:00.000Z"
        assert raw.payload["record_stage"] == "discovery_candidate"
        assert raw.payload["discovery_status"] == "unverified"
        assert raw.payload["content_status"] == "not_fetched"
        assert raw.payload["query_version"] == plan.query_version
        assert raw.payload["query_set_hash"] == plan.query_set_hash
        assert raw.payload["matched_queries"] == ["pallets/flask release"]
        assert raw.payload["discovery_provider"] == "hackernews"

    # 3. Manifest audit
    assert manifest["query_set_hash"] == plan.query_set_hash
    assert manifest["completeness_status"] == "complete"
    assert manifest["records_observed"] == 2
    assert manifest["records_rejected"] == 0
    assert len(manifest["per_query_stats"]) == 1
    assert manifest["per_query_stats"][0]["status"] == "ok"


def test_cutoff_safety_gate():
    """Verify that candidates with publication_date > cutoff_at are excluded and increment records_rejected."""
    c_valid = ExternalCandidate(
        title="Valid Flask Article Before Cutoff",
        url="https://example.com/flask-valid",
        source="hackernews",
        source_item_id="hn:2001",
        publication_date=datetime(2024, 1, 15, 0, 0, 0, tzinfo=UTC),
    )
    c_future = ExternalCandidate(
        title="Future Flask Article After Cutoff",
        url="https://example.com/flask-future",
        source="hackernews",
        source_item_id="hn:2002",
        publication_date=datetime(2024, 7, 1, 0, 0, 0, tzinfo=UTC),  # After cutoff
    )
    mock_provider = MockDiscoveryProvider(
        provider_name="hackernews",
        pages=[DiscoveryPageResult(candidates=[c_valid, c_future], page_number=1, has_more=False)],
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        clock=lambda: datetime(2024, 8, 1, 0, 0, 0, tzinfo=UTC),
    )

    cutoff_str = "2024-06-01T00:00:00.000Z"
    request = _make_request(cutoff_at=cutoff_str)
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask tutorial", intent="general", platform="hackernews")],
    )

    result, manifest = collector.collect_discovery(request, plan)

    # Future candidate must be excluded
    assert len(result.records) == 1
    assert result.records[0].source_record_id == "hn:2001"
    assert result.metadata.records_emitted == 1
    assert result.metadata.records_rejected == 1
    assert len(result.errors) >= 1
    assert result.errors[0].code == "validation_error"
    assert "cutoff" in result.errors[0].message

    # Must pass validate_record
    assert isinstance(validate_record(result.records[0]), RawRecord)
    assert isinstance(validate_record(result), CollectionResult)
    assert manifest["records_rejected"] == 1


def test_multi_query_upsert_and_provenance():
    """Verify that when 2 queries find the same source item, 1 RawRecord is emitted, matched_queries accumulates both, and subject_match upgrades."""
    # Query 1: broad alternative query
    # Candidate discovered first via generic query
    c_from_q1 = ExternalCandidate(
        title="Building Web Apps with Flask vs Django",
        url="https://example.com/flask-vs-django",
        source="hackernews",
        source_item_id="hn:3001",
        publication_date=datetime(2023, 5, 1, tzinfo=UTC),
        snippet="Django and Flask comparison",
    )
    # Query 2: exact repository query returning same item ID with exact github URL
    c_from_q2 = ExternalCandidate(
        title="Building Web Apps with Flask vs Django",
        url="https://example.com/flask-vs-django",
        source="hackernews",
        source_item_id="hn:3001",
        publication_date=datetime(2023, 5, 1, tzinfo=UTC),
        snippet="See repo at github.com/pallets/flask for details",  # Exact URL in snippet!
    )

    mock_provider = MockDiscoveryProvider(
        provider_name="hackernews",
        pages=[
            DiscoveryPageResult(candidates=[c_from_q1], page_number=1, has_more=False),
            DiscoveryPageResult(candidates=[c_from_q2], page_number=1, has_more=False),
        ],
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        clock=lambda: datetime(2024, 1, 1, tzinfo=UTC),
    )

    request = _make_request()
    term1 = SearchTerm(
        query="python web framework alternatives",
        intent="alternatives",
        platform="hackernews",
        context_level="alternative_level",
    )
    term2 = SearchTerm(
        query="pallets/flask",
        intent="direct_package",
        platform="hackernews",
        context_level="package_specific",
    )
    plan = SearchPlan(
        package_id="pypi:flask",
        repo_full_name="pallets/flask",
        terms=[term1, term2],
    )
    repo_id_obj = _make_repo_identity()

    result, _ = collector.collect_discovery(request, plan, repo_identity=repo_id_obj)

    # Exactly 1 RawRecord must be emitted
    assert len(result.records) == 1
    raw = result.records[0]
    assert raw.source_record_id == "hn:3001"

    # Both queries must be in matched_queries
    matched = raw.payload["matched_queries"]
    assert term1.query in matched
    assert term2.query in matched

    # Status must upgrade from function_or_alternative_only to exact_url_match
    assert raw.payload["subject_match_status"] == "exact_url_match"

    assert isinstance(validate_record(raw), RawRecord)
    assert isinstance(validate_record(result), CollectionResult)


def test_pagination_and_coverage_integrity():
    """Verify that when has_more=True and max_pages is reached, pagination_complete=False, next_cursor is populated, and coverage is partial."""
    c1 = ExternalCandidate(
        title="Page 1 Item",
        url="https://example.com/p1",
        source="hackernews",
        source_item_id="hn:4001",
        publication_date=datetime(2023, 1, 1, tzinfo=UTC),
    )
    mock_provider = MockDiscoveryProvider(
        provider_name="hackernews",
        pages=[DiscoveryPageResult(candidates=[c1], page_number=1, has_more=True, next_cursor="2")],
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        clock=lambda: datetime(2024, 1, 1, tzinfo=UTC),
    )

    # Limit to 1 page even though has_more is True
    request = _make_request(max_pages=1)
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask", intent="general", platform="hackernews")],
    )

    result, manifest = collector.collect_discovery(request, plan)

    assert result.metadata.pagination_complete is False
    assert result.metadata.next_cursor == "2"
    assert result.metadata.coverage[0].status == "partial"
    assert result.metadata.coverage[0].missing_reason == "not_collected"
    assert result.status == "partial"
    assert manifest["pagination_complete"] is False
    assert manifest["completeness_status"] == "partial"

    assert isinstance(validate_record(result), CollectionResult)


def test_historical_source_gate():
    """Verify that when historical_mode=True on an unsupported source (e.g., RSS), coverage is unavailable with historical_unavailable."""
    rss_provider = RSSProvider()
    assert rss_provider.supports_historical_window is False

    collector = ExternalDiscoveryCollector(
        providers={"rss": rss_provider},
        clock=lambda: datetime(2024, 1, 1, tzinfo=UTC),
    )

    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask feed", intent="announcement", platform="rss")],
    )

    result, manifest = collector.collect_discovery(
        request, plan, historical_mode=True
    )

    assert len(result.records) == 0
    assert result.metadata.records_emitted == 0
    assert result.metadata.coverage[0].status == "unavailable"
    assert result.metadata.coverage[0].missing_reason == "historical_unavailable"
    assert result.metadata.coverage[0].records_observed == 0
    assert manifest["completeness_status"] == "unavailable"
    assert len(result.errors) >= 1
    assert result.errors[0].code == "insufficient_history"

    assert isinstance(validate_record(result), CollectionResult)


def test_provider_error_handling_marks_partial_and_records_progress(tmp_path):
    """Verify that an error on provider marks coverage as partial and updates progress accounting."""
    manager = RunManager(tmp_path)
    progress = CollectionProgress(manager.start_run("err-run"), manager)

    mock_provider = MockDiscoveryProvider(
        provider_name="hackernews",
        error_to_raise=HTTPClientError("500 Internal Server Error", status_code=500),
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        progress=progress,
    )

    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask error test", intent="general", platform="hackernews")],
    )

    result, manifest = collector.collect_discovery(request, plan)

    assert result.status == "failed"
    assert result.metadata.coverage[0].status == "unavailable"
    assert len(result.errors) == 1
    assert result.errors[0].code == "http_error"
    assert result.errors[0].retryable is True
    assert progress.context.error_count == 1

    assert isinstance(validate_record(result), CollectionResult)


def test_save_discovery_records_persists_to_jsonl(tmp_path):
    """Verify save_discovery_records appends contract-compliant RawRecords to JSONL."""
    c1 = ExternalCandidate(
        title="Flask Article",
        url="https://example.com/flask-save-test",
        source="hackernews",
        source_item_id="hn:5001",
        publication_date=datetime(2023, 8, 1, tzinfo=UTC),
    )
    mock_provider = MockDiscoveryProvider(
        provider_name="hackernews",
        pages=[DiscoveryPageResult(candidates=[c1], page_number=1, has_more=False)],
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        clock=lambda: datetime(2024, 1, 1, tzinfo=UTC),
    )

    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask", intent="general", platform="hackernews")],
    )

    result, _ = collector.collect_discovery(request, plan)

    target_file = tmp_path / "raw" / "external" / "discovery.jsonl"
    storage = JsonlStorage()
    saved_path = collector.save_discovery_records(result, target_path=target_file, storage=storage)

    assert saved_path == target_file
    assert target_file.exists()

    stored_records = storage.read_contract_records(target_file)
    assert len(stored_records) == 1
    assert isinstance(stored_records[0], RawRecord)
    assert stored_records[0].source_record_id == "hn:5001"
    assert stored_records[0].source_url == "https://example.com/flask-save-test"


def test_invalid_url_candidate_is_rejected():
    """Verify candidate with non-canonicalizable URL is rejected and counted."""
    c_good = ExternalCandidate(
        title="Good URL",
        url="https://example.com/valid",
        source="hackernews",
        source_item_id="hn:6001",
    )
    c_bad = ExternalCandidate(
        title="Bad URL",
        url="http://invalid url with spaces.com",
        source="hackernews",
        source_item_id="hn:6002",
    )
    mock_provider = MockDiscoveryProvider(
        pages=[DiscoveryPageResult(candidates=[c_good, c_bad], page_number=1, has_more=False)],
    )

    collector = ExternalDiscoveryCollector(providers={"hackernews": mock_provider})
    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask", intent="general", platform="hackernews")],
    )

    result, _ = collector.collect_discovery(request, plan)
    assert len(result.records) == 1
    assert result.metadata.records_emitted == 1
    assert result.metadata.records_rejected == 1
    assert any(e.code == "validation_error" for e in result.errors)
    assert isinstance(validate_record(result), CollectionResult)


def test_rate_limit_error_sets_partial_coverage():
    """Verify RateLimitExceededError results in partial/unavailable coverage and retryable DataError."""
    from reposeer.exceptions import RateLimitExceededError

    mock_provider = MockDiscoveryProvider(
        error_to_raise=RateLimitExceededError("Rate limited", reset_timestamp=1700000000.0),
    )
    collector = ExternalDiscoveryCollector(providers={"hackernews": mock_provider})
    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask", intent="general", platform="hackernews")],
    )

    result, _ = collector.collect_discovery(request, plan)
    assert result.status == "failed"
    assert result.metadata.coverage[0].status == "unavailable"
    assert result.errors[0].code == "rate_limited"
    assert result.errors[0].retryable is True
    assert isinstance(validate_record(result), CollectionResult)


def test_time_range_rejection_and_admission():
    """Verify half-open time range (start_at, end_at] admits inside items and rejects outside items."""
    t_range = TimeRange(
        start_at="2023-01-01T00:00:00.000Z",
        end_at="2023-12-31T23:59:59.000Z",
    )
    c_in = ExternalCandidate(
        title="Inside window",
        url="https://example.com/inside",
        source="hackernews",
        source_item_id="hn:7001",
        publication_date=datetime(2023, 6, 1, tzinfo=UTC),
    )
    c_too_old = ExternalCandidate(
        title="Too old",
        url="https://example.com/old",
        source="hackernews",
        source_item_id="hn:7002",
        publication_date=datetime(2022, 12, 31, tzinfo=UTC),
    )
    mock_provider = MockDiscoveryProvider(
        pages=[DiscoveryPageResult(candidates=[c_in, c_too_old], page_number=1, has_more=False)],
    )

    collector = ExternalDiscoveryCollector(
        providers={"hackernews": mock_provider},
        clock=lambda: datetime(2024, 1, 1, tzinfo=UTC),
    )
    request = _make_request(time_range=t_range)
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask", intent="general", platform="hackernews")],
    )

    result, _ = collector.collect_discovery(request, plan)
    assert len(result.records) == 1
    assert result.records[0].source_record_id == "hn:7001"
    assert result.metadata.records_rejected == 1
    assert isinstance(validate_record(result), CollectionResult)


def test_collect_method_alias():
    """Verify collector.collect() returns CollectionResult directly."""
    c = ExternalCandidate(
        title="Flask Item",
        url="https://example.com/flask-item",
        source="hackernews",
        source_item_id="hn:8001",
    )
    mock_provider = MockDiscoveryProvider(
        pages=[DiscoveryPageResult(candidates=[c], page_number=1, has_more=False)],
    )
    collector = ExternalDiscoveryCollector(providers={"hackernews": mock_provider})
    request = _make_request()
    plan = SearchPlan(
        package_id="pypi:flask",
        terms=[SearchTerm(query="flask", intent="general", platform="hackernews")],
    )

    result = collector.collect(request, plan)
    assert isinstance(result, CollectionResult)
    assert len(result.records) == 1

