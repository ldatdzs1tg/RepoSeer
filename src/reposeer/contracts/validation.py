"""Strict record validation and request/result boundary checks."""

from urllib.parse import urlsplit

from reposeer.contracts.identity import add_calendar_months, canonical_github_url
from reposeer.contracts.models import (
    CONTRACT_ADAPTER,
    CollectionRequest,
    CollectionResult,
    ContractModel,
    ContractRecord,
    NormalizationRequest,
    NormalizationResult,
    SnapshotBuildRequest,
    SnapshotBuildResult,
    TechnologyLinkRequest,
    TechnologyLinkResult,
)


def validate_record(data: dict | str | bytes | ContractModel) -> ContractRecord:
    # Revalidate models too: model_construct, mutation and model_copy bypass validation.
    if isinstance(data, ContractModel):
        data = data.model_dump(mode="python", warnings=False)
    if isinstance(data, str | bytes):
        return CONTRACT_ADAPTER.validate_json(data, strict=True)
    return CONTRACT_ADAPTER.validate_python(data, strict=True)


def _pair(request, result) -> None:
    validate_record(request)
    validate_record(result)
    for field in ("schema_version", "request_id", "run_id"):
        if getattr(request, field) != getattr(result, field):
            raise ValueError(f"request/result {field} mismatch")


def validate_collection_pair(request: CollectionRequest, result: CollectionResult) -> None:
    _pair(request, result)
    metadata = result.metadata
    if result.source != request.source or metadata.collector_name != request.collector_name:
        raise ValueError("collection source/collector mismatch")
    if metadata.requested_time_range != request.time_range:
        raise ValueError("requested time range mismatch")
    if metadata.pages_fetched > request.max_pages or len(result.records) > request.max_records:
        raise ValueError("collection exceeded request budget")
    if metadata.records_rejected and not result.errors:
        raise ValueError("rejected records require errors")
    for coverage in metadata.coverage:
        if coverage.time_range is not None and coverage.time_range != request.time_range:
            raise ValueError("coverage time range mismatch")
        if (
            request.time_range is not None
            and coverage.status == "complete"
            and coverage.time_range is None
        ):
            raise ValueError("historical coverage requires the requested time range")
    for raw in result.records:
        if raw.source_record_type not in request.record_kinds:
            raise ValueError("unrequested record kind")
        if request.time_range is not None:
            if (
                raw.event_at is None
                or not request.time_range.start_at < raw.event_at <= request.time_range.end_at
            ):
                raise ValueError("record outside requested historical range")
        if request.cutoff_at is not None and (
            raw.available_at is None
            or raw.available_at > request.cutoff_at
            or (raw.event_at is not None and raw.event_at > request.cutoff_at)
        ):
            raise ValueError("record unavailable at requested cutoff")

        def matches(subject, raw=raw):
            if subject.repo_id is not None:
                return raw.repo_id == subject.repo_id
            if subject.repo_full_name is not None:
                observed = raw.payload.get("full_name")
                if (
                    isinstance(observed, str)
                    and observed.casefold() == subject.repo_full_name.casefold()
                ):
                    return True
                return (
                    raw.source_url.rstrip("/")
                    .casefold()
                    .endswith("/repos/" + subject.repo_full_name.casefold())
                )
            if subject.url is not None:
                if subject.url == raw.source_url:
                    return True
                observed = raw.payload.get("full_name")
                if isinstance(observed, str) and subject.url == canonical_github_url(observed):
                    return True
                parts = urlsplit(subject.url)
                return (
                    parts.hostname == "github.com"
                    and not parts.query
                    and not parts.fragment
                    and raw.source_url.rstrip("/")
                    .casefold()
                    .endswith("/repos/" + parts.path.strip("/").casefold())
                )
            if subject.package_id is not None:
                return raw.package_id == subject.package_id
            return False

        if not any(matches(subject) for subject in request.subjects):
            raise ValueError("record outside requested subjects")


def validate_normalization_pair(request: NormalizationRequest, result: NormalizationResult) -> None:
    _pair(request, result)
    if result.input_record_count != len(request.records):
        raise ValueError("normalization input count mismatch")
    inputs = {raw.raw_record_id: raw for raw in request.records}
    processed = set()
    for record in result.records:
        ids = set(record.provenance.raw_record_ids)
        if not ids <= inputs.keys():
            raise ValueError("orphan normalization raw reference")
        for raw_id in ids:
            raw = inputs[raw_id]
            if (
                record.provenance.source != raw.source
                or record.provenance.retrieved_at < raw.retrieved_at
            ):
                raise ValueError("normalization provenance mismatch")
            if record.record_type == "github_repository" and (
                record.identity.repo_id != raw.repo_id
                or record.provenance.retrieved_at != raw.retrieved_at
            ):
                raise ValueError("normalized repository identity/time mismatch")
        processed.update(ids)
    if len(processed) != result.processed_record_count:
        raise ValueError("processed count does not match referenced raw inputs")
    rejected = inputs.keys() - processed
    error_ids = {error.raw_record_id for error in result.errors if error.raw_record_id is not None}
    if not error_ids <= inputs.keys() or not rejected <= error_ids:
        raise ValueError("rejected inputs need errors referencing requested raw IDs")


def validate_technology_link_pair(
    request: TechnologyLinkRequest, result: TechnologyLinkResult
) -> None:
    _pair(request, result)
    articles = {article.article_id: article for article in request.articles}
    taxonomy = {technology.technology_id for technology in request.technology_entities}
    returned = {article.article_id for article in result.articles}
    if not returned <= articles.keys():
        raise ValueError("linker emitted a new article ID")
    if result.status == "complete" and returned != articles.keys():
        raise ValueError("complete linking must cover all input articles")
    for article in result.articles:
        if article.model_dump(exclude={"technology_mentions"}) != articles[
            article.article_id
        ].model_dump(exclude={"technology_mentions"}):
            raise ValueError("linker mutated immutable article fields")
        if any(mention.technology_id not in taxonomy for mention in article.technology_mentions):
            raise ValueError("unknown technology ID in linked article")


def validate_snapshot_pair(request: SnapshotBuildRequest, result: SnapshotBuildResult) -> None:
    _pair(request, result)
    for coverage in request.coverage:
        if coverage.time_range is not None and coverage.time_range.end_at > request.cutoff_at:
            raise ValueError("snapshot coverage extends past cutoff")
    snapshot = result.snapshot
    if snapshot is None:
        return
    if (
        snapshot.package_id != request.package_id
        or snapshot.repo_id != request.repository.repo_id
        or snapshot.cutoff_at != request.cutoff_at
        or snapshot.horizon_months != request.horizon_months
        or snapshot.horizon_end_at != add_calendar_months(request.cutoff_at, request.horizon_months)
    ):
        raise ValueError("snapshot grain/horizon mismatch")
    raw_ids = {
        raw_id
        for record in [*request.releases, *request.activities]
        for raw_id in record.provenance.raw_record_ids
    }
    if set(snapshot.raw_record_ids) != raw_ids:
        raise ValueError("snapshot provenance must match requested evidence")
    if snapshot.coverage != request.coverage:
        raise ValueError("snapshot coverage mismatch")
    # Unobserved groups cannot become known feature values, including zero.
    groups = {coverage.data_group: coverage.status for coverage in request.coverage}
    feature_groups = {
        "package_age_days": "releases",
        "releases_total": "releases",
        "days_since_latest_release": "releases",
        "contributor_count_12m": "contributors",
    }
    for name, value in snapshot.features.model_dump().items():
        group = feature_groups.get(name)
        if name.startswith("release_count_"):
            group = "releases"
        elif name.startswith("commit_count_"):
            group = "commits"
        elif name.startswith("issue_"):
            group = "issues"
        elif name.startswith("pr_"):
            group = "pull_requests"
        if value is not None and groups.get(group) != "complete":
            raise ValueError("unavailable/incomplete feature group cannot become a known value")
