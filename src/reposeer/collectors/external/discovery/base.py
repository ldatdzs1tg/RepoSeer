"""External discovery collector orchestrating queries, pagination, and canonical envelopes."""

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field

from reposeer.clients.external.base import DiscoveryProvider, is_historical_discovery
from reposeer.collection.persistence import write_json_atomic
from reposeer.collection.progress import CollectionProgress
from reposeer.contracts import (
    CollectionMetadata,
    CollectionRequest,
    CollectionResult,
    CollectionSubject,
    Coverage,
    DataError,
    RawRecord,
    RepoIdentity,
    validate_record,
)
from reposeer.contracts.identity import (
    canonical_article_url,
    format_utc_timestamp,
    parse_repo_id,
)
from reposeer.contracts.identity import (
    package_id as normalize_package_id,
)
from reposeer.contracts.validation import validate_collection_pair
from reposeer.exceptions import HTTPClientError, RateLimitExceededError
from reposeer.processing.normalize.timestamps import normalize_datetime
from reposeer.schemas.external.candidate import (
    ExternalCandidate,
    SubjectMatchStatus,
    evaluate_subject_match,
)
from reposeer.storage.paths import paths
from reposeer.storage.raw.jsonl import JsonlStorage
from reposeer.technology.models import SearchPlan, compute_query_set_hash

_STATUS_RANK: dict[SubjectMatchStatus, int] = {
    "exact_url_match": 4,
    "high_confidence_metadata": 3,
    "ambiguous_requires_crawl": 2,
    "function_or_alternative_only": 1,
    "technology_context_only": 1,
    "rejected_noise": 0,
}


def _canonical_package_id(val: str | None) -> str | None:
    if not val or not isinstance(val, str):
        return None
    val_clean = val.strip()
    if not val_clean:
        return None
    name = val_clean[5:] if val_clean.startswith("pypi:") else val_clean
    try:
        return normalize_package_id(name)
    except ValueError:
        return None


def _canonical_repo_id(val: str | None) -> str | None:
    if not val or not isinstance(val, str):
        return None
    val_clean = val.strip()
    if val_clean.startswith("github:repository:"):
        parse_repo_id(val_clean)
        return val_clean
    return None


SOURCE_POLICY_VERSION = "2.0.0"
DISCOVERY_STATE_VERSION = 1
TOTAL_REQUEST_LIMIT = 10000


class DiscoveryJob(BaseModel):
    """One provider/query/window retrieval and its durable progress, not a workflow engine."""

    model_config = ConfigDict(extra="forbid", strict=True)

    job_id: str
    provider: str
    query_id: str
    query: str
    query_intent: str
    context_level: str
    tech_slug: str | None = None
    requested_window: dict[str, str | None]
    execution_identity: dict[str, Any]
    status: Literal["pending", "running", "complete", "partial", "failed", "unavailable", "skipped_budget"] = "pending"
    attempted: bool = False
    requests_attempted: int = Field(default=0, ge=0)
    pages_fetched: int = Field(default=0, ge=0)
    records_observed: int = Field(default=0, ge=0)
    native_items_observed: int = Field(default=0, ge=0)
    records_rejected: int = Field(default=0, ge=0)
    candidate_ids: list[str] = Field(default_factory=list)
    last_completed_cursor: str | None = None
    next_cursor: str | None = None
    has_more: bool = True
    prefix_unobserved: bool = False
    errors: list[dict[str, Any]] = Field(default_factory=list)
    error_history: list[dict[str, Any]] = Field(default_factory=list)
    missing_reason: str | None = None
    total_hits_reported: int | None = None
    observed_from: str | None = None
    observed_to: str | None = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def load_discovery_state(path: Path) -> dict[str, Any]:
    """Reject legacy mutable-raw state rather than reinterpret its observations."""
    state = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(state, dict) or type(state.get("state_format_version")) is not int
            or state["state_format_version"] != DISCOVERY_STATE_VERSION):
        raise ValueError("Incompatible discovery state format; start a new run")
    if (not isinstance(state.get("scope"), dict)
            or not all(key in state["scope"] for key in (*input_observation_scope(None), "include_technology_context"))
            or not all(isinstance(state.get(key), list) for key in ("records", "candidate_projections", "jobs"))):
        raise ValueError("Invalid discovery state scope or observations; start a new run")
    return state


def input_observation_scope(raw: RawRecord | None) -> dict[str, Any]:
    """Bind recorded input to its full validated snapshot, independently of HTTP body hashes."""
    if raw is not None:
        raw = validate_record(raw)
        if not isinstance(raw, RawRecord):
            raise TypeError("Expected input RawRecord")
    return {
        "input_raw_record_id": raw.raw_record_id if raw else None,
        "input_source": raw.source if raw else None,
        "input_source_record_type": raw.source_record_type if raw else None,
        "input_repo_id": raw.repo_id if raw else None,
        "input_repo_full_name": raw.payload.get("full_name") if raw else None,
        "input_observation_fingerprint": _hash(raw.model_dump(mode="json")) if raw else None,
    }


def _source_observation_key(payload: dict[str, Any]) -> str:
    # Compare only actual upstream metadata, never query lineage, match rank or retrieval clock.
    fields = ("source", "title", "url", "source_item_id", "source_item_url", "publication_date",
              "score", "snippet", "author", "comment_count", "native_payload")
    return _hash({name: payload.get(name) for name in fields})


def _error(code: str, message: str, *, retryable: bool = False, source_item_id: str | None = None) -> DataError:
    return DataError(code=code, message=message, retryable=retryable,
                     source_record_id=source_item_id, raw_record_id=None)


def _observation_url(candidate: ExternalCandidate, provider: DiscoveryProvider, *, windowed: bool) -> str:
    if candidate.source_item_url:
        return canonical_article_url(candidate.source_item_url)
    if candidate.source == "hackernews" and candidate.source_item_id and candidate.source_item_id.startswith("hn:"):
        native_id = candidate.source_item_id.removeprefix("hn:")
        if native_id.isdecimal():
            return f"https://news.ycombinator.com/item?id={native_id}"
    origin = provider.execution_policy().get("base_url")
    if origin:
        suffix = {"hackernews": "/search_by_date" if windowed else "/search", "devto": "/articles/search", "stackexchange": "/search/advanced"}.get(candidate.source, "")
        return canonical_article_url(origin.rstrip("/") + suffix)
    # A custom provider must supply an attributable origin; no article-fetch inference.
    raise ValueError("provider observation URL is unavailable")


class ExternalDiscoveryCollector:
    """Orchestrates multi-query external discovery with cutoff-safety and canonical record generation."""

    def __init__(
        self,
        providers: Mapping[str, DiscoveryProvider],
        *,
        progress: CollectionProgress | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.providers = dict(providers)
        self.progress = progress
        self.clock = clock or (lambda: datetime.now(UTC))

    def collect(
        self,
        request: CollectionRequest,
        search_plan: SearchPlan,
        *,
        repo_identity: RepoIdentity | None = None,
        package_id: str | None = None,
        historical_mode: bool = False,
    ) -> CollectionResult:
        """Alias delegating to collect_discovery and returning CollectionResult."""
        result, _ = self.collect_discovery(
            request,
            search_plan,
            repo_identity=repo_identity,
            package_id=package_id,
            historical_mode=historical_mode,
        )
        return result

    def collect_discovery(
        self,
        request: CollectionRequest,
        search_plan: SearchPlan,
        *,
        repo_identity: RepoIdentity | None = None,
        package_id: str | None = None,
        historical_mode: bool = False,
        input_observation: RawRecord | None = None,
    ) -> tuple[CollectionResult, dict[str, Any]]:
        """Execute planned search queries across configured discovery providers.

        Respects cutoff bounds, maintains candidate projections, and emits
        canonical RawRecord envelopes with coverage manifest audit details.
        """
        request = validate_record(request)
        if not isinstance(request, CollectionRequest):
            raise TypeError("Expected CollectionRequest")

        if len(request.subjects) != 1:
            raise ValueError("Discovery accepts one search target per logical run")
        if repo_identity is not None:
            repo_identity = RepoIdentity.model_validate(repo_identity.model_dump())
        subject = request.subjects[0]
        if request.source != "web_article" or request.record_kinds != ["article"]:
            raise ValueError("Discovery requires web_article/article observation envelopes")
        if search_plan.query_set_hash != compute_query_set_hash(search_plan.terms):
            raise ValueError("SearchPlan query_set_hash does not match planned terms")
        started = format_utc_timestamp(self.clock())
        state_path = (self.progress.run_manager.checkpoints_dir / f"discovery_state_{request.run_id}.json"
                      if self.progress and self.progress.run_manager else None)
        saved = load_discovery_state(state_path) if state_path is not None and state_path.exists() else None
        initial_cursor = request.cursor if saved is None else saved["scope"].get("initial_cursor")
        cutoff = normalize_datetime(request.cutoff_at) if request.cutoff_at else None
        start = normalize_datetime(request.time_range.start_at) if request.time_range else None
        end = normalize_datetime(request.time_range.end_at) if request.time_range else None
        effective_end = min(value for value in (cutoff, end) if value is not None) if (cutoff or end) else None
        historical = is_historical_discovery(cutoff_at=cutoff, window_end=end, now=self.clock(), explicit=historical_mode)
        if saved is not None:
            if historical_mode and not saved["scope"]["historical_mode"]:
                raise ValueError("Resume temporal mode differs from saved run")
            historical = saved["scope"]["historical_mode"]
        target_package = _canonical_package_id(package_id or search_plan.package_id or subject.package_id)
        target_repo = repo_identity.repo_id if repo_identity else _canonical_repo_id(search_plan.repo_id or subject.repo_id)
        full_name = repo_identity.repo_full_name if repo_identity else search_plan.repo_full_name or subject.repo_full_name
        if (subject.package_id is not None and target_package != subject.package_id
                or subject.repo_id is not None and target_repo != subject.repo_id
                or subject.repo_full_name is not None and (not full_name or full_name.casefold() != subject.repo_full_name.casefold())):
            raise ValueError("Search plan and collection request subjects disagree")
        window = {"start_at": format_utc_timestamp(start) if start else None,
                  "end_at": format_utc_timestamp(effective_end) if effective_end else None}
        jobs: list[DiscoveryJob] = []
        for term in search_plan.terms:
            targets = (list(self.providers) if term.platform in {"all", "web"}
                       else [term.platform])
            for name in sorted(targets or [term.platform]):
                provider = self.providers.get(name)
                policy = provider.execution_policy() if provider else {}
                # Include effective provider filters, not credentials or native cursors.
                policy.update(page_size=min(50, max(1, request.max_records // len(self.providers))) if len(self.providers) > 1 else 50, window_filter="(start,end]", historical_mode=historical, starting_cursor=initial_cursor)
                identity = {"provider": name, "query_id": term.query_id,
                            "query": " ".join(term.query.split()).casefold(), "query_intent": term.intent,
                            "context_level": term.context_level, "requested_window": window,
                            "query_version": term.query_version, "plan_query_version": search_plan.query_version,
                            "source_policy_version": SOURCE_POLICY_VERSION,
                            "filters": policy}
                if term.tech_slug is not None:
                    identity["tech_slug"] = term.tech_slug
                job = DiscoveryJob(job_id="job:" + _hash(identity), provider=name,
                                   query_id=term.query_id or "", query=term.query,
                                   query_intent=term.intent, context_level=term.context_level, tech_slug=term.tech_slug,
                                   requested_window=window, execution_identity=identity)
                if provider is None:
                    job.status, job.missing_reason = "unavailable", "no_provider"
                elif historical and not provider.supports_historical_window:
                    job.status, job.missing_reason = "unavailable", "historical_unavailable"
                elif not provider.is_available:
                    job.status, job.missing_reason = "unavailable", getattr(provider, "unavailable_reason", "provider_not_implemented")
                jobs.append(job)
        jobs = list({job.job_id: job for job in jobs}.values())
        scope = {"subject": [subject.model_dump() for subject in request.subjects],
                 "package_id": target_package, "repo_id": target_repo, "repo_full_name": full_name,
                 "planned_query_set_hash": compute_query_set_hash(search_plan.terms),
                 "query_version": search_plan.query_version, "cutoff_at": request.cutoff_at,
                 "window": window, "execution_policy_hash": _hash(sorted(job.job_id for job in jobs)),
                 "source_policy_version": SOURCE_POLICY_VERSION, "historical_mode": historical,
                 "initial_cursor": initial_cursor,
                 "include_technology_context": search_plan.include_technology_context,
                 **input_observation_scope(input_observation),
                 "providers": {name: provider.execution_policy() for name, provider in sorted(self.providers.items())}}
        raw_observations_by_id: dict[str, RawRecord] = {}
        candidate_projections_by_id: dict[str, ExternalCandidate] = {}
        observation_ids_by_content: dict[tuple[str, str], str] = {}
        if saved is not None:
            if saved["scope"] != scope:
                raise ValueError("Resume subject, query plan, provider policy, or window differs from saved run")
            restored = [DiscoveryJob.model_validate(job) for job in saved["jobs"]]
            if {job.job_id for job in restored} != {job.job_id for job in jobs}:
                raise ValueError("Resume job identities differ from saved execution plan")
            expected_jobs = {job.job_id: job for job in jobs}
            for job in restored:
                expected = expected_jobs[job.job_id]
                if (job.execution_identity != expected.execution_identity or job.query != expected.query
                        or job.provider != expected.provider or job.query_id != expected.query_id
                        or job.query_intent != expected.query_intent or job.context_level != expected.context_level
                        or job.tech_slug != expected.tech_slug):
                    raise ValueError("Saved job policy is inconsistent with execution identity")
            jobs = restored
            started = saved["started_at"]
            for raw_data in saved["records"]:
                raw = validate_record(raw_data)
                if not isinstance(raw, RawRecord) or raw.run_id != request.run_id:
                    raise ValueError("Invalid discovery replay record")
                previous = raw_observations_by_id.get(raw.raw_record_id)
                if previous is not None and _canonical_json(previous.model_dump(mode="json")) != _canonical_json(raw.model_dump(mode="json")):
                    raise ValueError("Raw observation integrity conflict in discovery state")
                raw_observations_by_id[raw.raw_record_id] = raw
                observation_ids_by_content[(str(raw.payload["candidate_id"]), _source_observation_key(raw.payload))] = raw.raw_record_id
            for data in saved["candidate_projections"]:
                candidate = ExternalCandidate.model_validate(data)
                if not candidate.raw_record_ids or any(
                    raw_id not in raw_observations_by_id
                    or raw_observations_by_id[raw_id].payload["candidate_id"] != candidate.candidate_id
                    for raw_id in candidate.raw_record_ids
                ):
                    raise ValueError("Invalid candidate projection raw references")
                candidate_projections_by_id[candidate.candidate_id] = candidate
        if len(raw_observations_by_id) > request.max_records:
            raise ValueError("Resume max_records is smaller than the number already persisted")
        for job in jobs:
            provider = self.providers.get(job.provider)
            if provider and job.next_cursor is not None:
                provider.validate_cursor(job.next_cursor)
            if job.status == "complete" and (job.has_more or job.next_cursor is not None):
                raise ValueError("Invalid completed job cursor")
        if request.cursor is not None:
            if state_path is not None and state_path.exists():
                raise ValueError("Resume uses saved job state; do not supply a provider cursor")
            if len(jobs) != 1:
                raise ValueError("Bare cursor is ambiguous for multiple jobs; use run_id resume")
            provider = self.providers.get(jobs[0].provider)
            if provider is None:
                raise ValueError("Cursor has no available target provider")
            provider.validate_cursor(request.cursor)
            jobs[0].next_cursor = request.cursor
            jobs[0].prefix_unobserved = int(request.cursor) > int(provider.initial_cursor)

        def persist() -> None:
            # Immutable observations, projections and cursors form one atomic, fsynced index.
            # A crash after this write can replay output without refetching completed pages.
            if state_path is not None:
                write_json_atomic(state_path, {"state_format_version": DISCOVERY_STATE_VERSION,
                                              "scope": scope, "started_at": started,
                                              "jobs": [job.model_dump() for job in jobs],
                                              "records": [record.model_dump(mode="json") for record in raw_observations_by_id.values()],
                                              "candidate_projections": [candidate.model_dump(mode="json")
                                                                        for candidate in candidate_projections_by_id.values()]})
                self.progress.context.set_checkpoint("external.discovery.state", state_path.name)
                self.progress.run_manager.save_checkpoint(self.progress.context)

        persist()
        provider_record_limit = max(1, request.max_records // max(1, len(self.providers)))

        def provider_count(name):
            return sum(raw.payload.get("discovery_provider") == name for raw in raw_observations_by_id.values())

        pages_this_call = 0
        requests_this_call = 0
        for job in jobs:
            if job.status == "unavailable" or not job.has_more:
                continue
            if len(raw_observations_by_id) >= request.max_records or requests_this_call >= TOTAL_REQUEST_LIMIT:
                job.status, job.missing_reason = "skipped_budget", "max_records" if len(raw_observations_by_id) >= request.max_records else "total_request_budget"
                persist()
                continue
            if len(self.providers) > 1 and provider_count(job.provider) >= provider_record_limit:
                job.status, job.missing_reason = "skipped_budget", "provider_record_budget"
                persist()
                continue
            provider = self.providers[job.provider]
            term = next(term for term in search_plan.terms if term.query_id == job.query_id and term.query == job.query
                        and term.intent == job.query_intent and term.context_level == job.context_level)
            job.status, job.missing_reason = "running", None
            # Retain audit history; only unresolved failures affect current coverage.
            job.errors = [error for error in job.errors if error["code"] == "validation_error"]
            pages_this_job = 0
            while pages_this_job < request.max_pages and requests_this_call < TOTAL_REQUEST_LIMIT:
                cursor = job.next_cursor
                before = getattr(getattr(provider, "http", None), "requests_attempted", None)
                try:
                    job.attempted = True
                    requests_this_call += 1
                    page = provider.search_page(job.query, window_start=start, window_end=effective_end,
                                                cursor=cursor, page_size=min(50, provider_record_limit) if len(self.providers) > 1 else 50)
                except (HTTPClientError, ValueError) as error:
                    job.requests_attempted += provider.http.requests_attempted - before if before is not None else 1
                    code = ("rate_limited" if isinstance(error, RateLimitExceededError)
                            or isinstance(error, HTTPClientError) and error.status_code == 429
                            else "http_error" if isinstance(error, HTTPClientError) else "parse_error")
                    if isinstance(error, HTTPClientError) and isinstance(error.__cause__, httpx.RequestError):
                        code = "network_error"
                    status = error.status_code if isinstance(error, HTTPClientError) else None
                    entry = _error(code, f"{job.provider}: {type(error.__cause__ or error).__name__}; HTTP status={status}",
                                   retryable=code == "network_error" or status in {429, 500, 502, 503, 504})
                    job.errors.append(entry.model_dump())
                    job.error_history.append(entry.model_dump())
                    job.status = "partial" if job.pages_fetched else "failed"
                    job.missing_reason = getattr(error, "code", code)
                    if self.progress:
                        self.progress.record_error()
                    persist()
                    break
                job.requests_attempted += provider.http.requests_attempted - before if before is not None else 1
                pages_this_job += 1
                pages_this_call += 1
                job.pages_fetched += 1
                job.records_observed += len(page.candidates)
                job.native_items_observed += page.native_items_observed if page.native_items_observed is not None else len(page.candidates)
                job.total_hits_reported = page.total_hits_reported
                invalid_pagination = False
                try:
                    if ((page.has_more and (page.next_cursor is None or page.next_cursor == cursor))
                            or (not page.has_more and page.next_cursor is not None)):
                        raise ValueError("missing or non-advancing cursor")
                    if page.next_cursor is not None:
                        provider.validate_cursor(page.next_cursor)
                except ValueError:
                    invalid_pagination = True
                    entry = _error("parse_error", f"{job.provider}: invalid pagination cursor")
                    job.errors.append(entry.model_dump())
                    job.error_history.append(entry.model_dump())
                interrupted = False
                for candidate in page.candidates:
                    try:
                        candidate = ExternalCandidate.model_validate(candidate.model_dump())
                        candidate.source = job.provider
                        candidate.url = canonical_article_url(candidate.url)
                        observed_at = self.clock()
                        if job.provider == "rss":
                            observed_at = candidate.discovered_at
                            if observed_at.tzinfo is None or observed_at > self.clock():
                                raise ValueError("invalid_feed_observation_time")
                        candidate.discovered_at = observed_at
                        pub = candidate.publication_date
                        if pub is not None and (pub.tzinfo is None or pub.utcoffset() is None):
                            raise ValueError("publication_date must be timezone aware")
                        if pub is not None and pub > observed_at:
                            raise ValueError("publication_date is after retrieval")
                        if pub is not None and effective_end is not None and pub > effective_end:
                            raise ValueError("publication_date is after cutoff/window_end")
                        if pub is not None and start is not None and pub <= start:
                            raise ValueError("publication_date is before or at window_start")
                        if pub is None and (start is not None or effective_end is not None):
                            raise ValueError("publication_date missing for requested window")
                        candidate_id = candidate.candidate_id
                        status = evaluate_subject_match(candidate, query_intent=term.intent,
                                                        context_level=term.context_level, repo_full_name=full_name,
                                                        repo_url=repo_identity.repo_url if repo_identity else None,
                                                        package_name=target_package.removeprefix("pypi:") if target_package else None,
                                                        primary_keywords=term.keywords,
                                                        known_urls=repo_identity.known_urls if repo_identity else None)
                        candidate.query, candidate.query_id = term.query, term.query_id
                        candidate.query_intent, candidate.context_level = term.intent, term.context_level
                        candidate.tech_slug = term.tech_slug
                        candidate.subject_match_status = status
                        association = {"query_id": job.query_id, "job_id": job.job_id, "provider": job.provider,
                                       "intent": term.intent, "context_level": term.context_level, "query": term.query}
                        if term.tech_slug is not None:
                            association["tech_slug"] = term.tech_slug
                        candidate.query_associations = [association]
                        candidate.matched_queries = [term.query]
                        pub = candidate.publication_date
                        payload = candidate.model_dump(mode="json")
                        origin = _observation_url(candidate, provider, windowed=start is not None or effective_end is not None)
                        payload.update(record_stage="discovery_candidate", discovery_status="unverified",
                                       content_status="not_fetched", discovery_provider=job.provider,
                                       candidate_url=candidate.url, observation_source_url=origin,
                                       search_target_package_id=target_package, search_target_repo_id=target_repo,
                                       search_target_repo_full_name=full_name, query_version=search_plan.query_version,
                                       query_set_hash=scope["planned_query_set_hash"], source_policy_version=SOURCE_POLICY_VERSION,
                                       window_start=window["start_at"], window_end=window["end_at"], cutoff_at=request.cutoff_at)
                        content_key = (candidate_id, _source_observation_key(payload))
                        raw_id = observation_ids_by_content.get(content_key)
                        if raw_id is None and (len(raw_observations_by_id) >= request.max_records
                                or len(self.providers) > 1 and provider_count(job.provider) >= provider_record_limit):
                            interrupted = True
                            break
                        if raw_id is None:
                            raw = RawRecord(schema_version="0.1.0", record_type="raw_record",
                                            raw_record_id=f"raw:{uuid4()}",
                                            run_id=request.run_id, source="web_article", source_record_type="article",
                                            source_record_id=candidate.source_item_id, repo_id=None, package_id=None,
                                            source_url=origin, retrieved_at=format_utc_timestamp(observed_at),
                                            event_at=format_utc_timestamp(pub) if pub is not None else None,
                                            available_at=None, http_status=None, raw_body_sha256=None, payload=payload)
                            raw = validate_record(raw)
                            raw_id = raw.raw_record_id
                            raw_observations_by_id[raw_id] = raw
                            observation_ids_by_content[content_key] = raw_id
                        # Only the projection accumulates query/rank metadata; raw payload stays unchanged.
                        previous = candidate_projections_by_id.get(candidate_id)
                        candidate.raw_record_ids = [raw_id]
                        if previous is not None:
                            associations = {entry["job_id"]: entry for entry in previous.query_associations + [association]}
                            raw_ids = list(dict.fromkeys([*previous.raw_record_ids, raw_id]))
                            if _STATUS_RANK[previous.subject_match_status] > _STATUS_RANK[status]:
                                candidate = previous.model_copy(deep=True)
                            for name in ("score", "snippet", "author", "comment_count"):
                                if getattr(candidate, name) is None:
                                    setattr(candidate, name, getattr(previous, name))
                            candidate.discovered_at = min(previous.discovered_at, candidate.discovered_at)
                            candidate.query_associations = sorted(associations.values(), key=lambda entry: entry["job_id"])
                            candidate.matched_queries = sorted({entry["query"] for entry in associations.values()})
                            candidate.raw_record_ids = raw_ids
                        candidate_projections_by_id[candidate_id] = candidate
                        if candidate_id not in job.candidate_ids:
                            job.candidate_ids.append(candidate_id)
                        if pub is not None:
                            stamp = format_utc_timestamp(pub)
                            job.observed_from = min(job.observed_from or stamp, stamp)
                            job.observed_to = max(job.observed_to or stamp, stamp)
                    except ValueError as error:
                        job.records_rejected += 1
                        detail = str(error) if str(error).startswith("publication_date") else "Invalid candidate metadata or provider origin"
                        entry = _error("validation_error", detail, source_item_id=candidate.source_item_id)
                        job.errors.append(entry.model_dump())
                        job.error_history.append(entry.model_dump())
                if invalid_pagination:
                    job.status, job.missing_reason = "partial", "invalid_cursor"
                    persist()
                    break
                if interrupted:
                    job.status, job.missing_reason = "partial", "max_records"
                    # Retry this same page. Admitted observations deduplicate by native content, independent of query.
                    persist()
                    break
                job.last_completed_cursor = cursor or provider.initial_cursor
                job.next_cursor, job.has_more = page.next_cursor, page.has_more
                job.status = "partial" if page.has_more or job.errors or job.prefix_unobserved else "complete"
                job.missing_reason = ("max_pages" if page.has_more else "invalid_items" if job.errors
                                      else "cursor_prefix_not_collected" if job.prefix_unobserved else None)
                persist()
                if not page.has_more:
                    break
                if (len(raw_observations_by_id) >= request.max_records
                        or len(self.providers) > 1 and provider_count(job.provider) >= provider_record_limit):
                    job.status, job.missing_reason = "partial", "max_records"
                    persist()
                    break
            if job.has_more and job.missing_reason == "max_pages" and requests_this_call >= TOTAL_REQUEST_LIMIT:
                job.missing_reason = "total_request_budget"
        persist()
        errors = [DataError.model_validate(error) for job in jobs for error in job.errors]
        for job in jobs:
            if job.status == "unavailable":
                errors.append(_error("insufficient_history" if job.missing_reason == "historical_unavailable"
                                     else "unsupported_record_type", f"{job.provider}: {job.missing_reason}"))
        if not jobs:
            errors.append(_error("unsupported_record_type", "No executable discovery jobs"))
        pagination_complete = bool(jobs) and all(not job.has_more and not job.prefix_unobserved and job.status not in {"unavailable", "skipped_budget"} for job in jobs)
        rejected = sum(job.records_rejected for job in jobs)
        complete = bool(jobs) and all(job.status == "complete" for job in jobs)
        # Pagination is about today's API response, not an authenticated historical corpus.
        historical_limit = historical and any(job.pages_fetched for job in jobs)
        coverage_status = "complete" if complete and not historical_limit else "partial" if raw_observations_by_id or any(job.pages_fetched for job in jobs) else "unavailable"
        missing = (None if coverage_status == "complete" else "historical_unavailable"
                   if historical_limit or any(job.missing_reason == "historical_unavailable" for job in jobs)
                   else "not_collected" if coverage_status == "partial" else "unavailable")
        result_status = "complete" if coverage_status == "complete" else "partial" if coverage_status == "partial" or raw_observations_by_id or not errors else "failed"
        unfinished = [job for job in jobs if job.has_more and job.status != "unavailable"]
        next_cursor = None
        if unfinished:
            first = unfinished[0]
            native = first.next_cursor or self.providers[first.provider].initial_cursor
            next_cursor = native if len(jobs) == 1 else json.dumps({"job_id": first.job_id, "cursor": native}, sort_keys=True)
        metadata = CollectionMetadata(collector_name=request.collector_name, collector_version="0.2.0",
                                      started_at=min([started] + [raw.retrieved_at for raw in raw_observations_by_id.values()]),
                                      finished_at=format_utc_timestamp(self.clock()), requested_time_range=request.time_range,
                                      pages_fetched=pages_this_call, records_emitted=len(raw_observations_by_id), records_rejected=rejected,
                                      pagination_complete=pagination_complete, next_cursor=next_cursor,
                                      coverage=[Coverage(data_group="articles", status=coverage_status, time_range=request.time_range,
                                                         records_observed=len(raw_observations_by_id) if coverage_status != "unavailable" else 0,
                                                         missing_reason=missing)], rate_limit_remaining=None, rate_limit_reset_at=None)
        result = CollectionResult(schema_version="0.1.0", record_type="collection_result",
                                  request_id=request.request_id, run_id=request.run_id, source=request.source,
                                  status=result_status, records=list(raw_observations_by_id.values()), errors=errors,
                                  metadata=metadata)
        result = validate_record(result)
        self.validate_observation_pair(request, result, job_count=len(jobs))
        attempted = [job.execution_identity for job in jobs if job.attempted]
        per_job = [dict(job.model_dump(), intent=job.query_intent, platform=job.provider,
                        candidates_found=len(job.candidate_ids), window=job.requested_window) for job in jobs]
        manifest = {"run_id": request.run_id, "request_id": request.request_id,
                    "query_set_hash": scope["planned_query_set_hash"], "planned_query_set_hash": scope["planned_query_set_hash"],
                    "actual_query_set_hash": _hash(sorted(attempted, key=lambda entry: _hash(entry))),
                    "source_policy_version": SOURCE_POLICY_VERSION, "execution_policy_hash": scope["execution_policy_hash"],
                    "query_version": search_plan.query_version, "profile_status": search_plan.profile_status,
                    "include_technology_context": search_plan.include_technology_context,
                    "candidate_projections": [candidate.model_dump(mode="json") for candidate in candidate_projections_by_id.values()],
                    "candidate_projection_path": str(state_path) if state_path else None,
                    "candidate_count": len(candidate_projections_by_id),
                    "diagnostics": search_plan.diagnostics, "temporal_mode": "historical" if historical else "current",
                    "package_id": target_package, "repo_id": target_repo,
                    "search_target_package_id": target_package, "search_target_repo_id": target_repo,
                    "search_target_repo_full_name": full_name, "requested_window_start": window["start_at"],
                    "requested_window_end": window["end_at"], "per_job_stats": per_job, "per_query_stats": per_job,
                    "observed_from": min((job.observed_from for job in jobs if job.observed_from), default=None),
                    "observed_to": max((job.observed_to for job in jobs if job.observed_to), default=None),
                    "pagination_complete": pagination_complete, "retrieval_complete": complete, "completeness_status": coverage_status,
                    "records_observed": len(raw_observations_by_id), "records_rejected": rejected,
                    "errors": [error.model_dump() for error in errors],
                    "provider_record_limit": provider_record_limit,
                    "max_pages_scope": "per_provider_query_per_invocation", "max_records_scope": "logical_run_raw_observations",
                    "max_pages_per_job": request.max_pages, "max_records": request.max_records,
                    "total_request_limit": TOTAL_REQUEST_LIMIT, "requests_this_invocation": requests_this_call, "historical_content_verified": False,
                    "coverage_basis": "provider_search_pagination",
                    "limitations": ["Provider search is not exhaustive historical corpus or content-version proof"]}
        return result, manifest

    @staticmethod
    def validate_observation_pair(request: CollectionRequest, result: CollectionResult, *, job_count: int) -> None:
        """Validate discovery as observations, not as verified target/as-of article evidence.

        Canonical pair validation requires verified subject matches and availability at cutoff.
        Keep the actual request/result intact and validate an observation-scope projection;
        search targets and publication windows are separately checked by this collector.
        """
        validate_record(request)
        validate_record(result)
        if result.metadata.requested_time_range != request.time_range:
            raise ValueError("Discovery request/result window mismatch")
        for raw in result.records:
            target = request.subjects[0]
            if (raw.repo_id is not None or raw.package_id is not None or raw.available_at is not None
                    or raw.payload.get("record_stage") != "discovery_candidate"):
                raise ValueError("Discovery cannot claim a verified subject or historical content")
            if (target.package_id is not None and raw.payload.get("search_target_package_id") != target.package_id
                    or target.repo_id is not None and raw.payload.get("search_target_repo_id") != target.repo_id
                    or target.repo_full_name is not None and str(raw.payload.get("search_target_repo_full_name", "")).casefold() != target.repo_full_name.casefold()):
                raise ValueError("Discovery search-target lineage mismatch")
            if request.time_range is not None and (raw.event_at is None or not request.time_range.start_at < raw.event_at <= request.time_range.end_at):
                raise ValueError("Candidate publication outside requested discovery window")
            if request.cutoff_at is not None and raw.event_at is not None and raw.event_at > request.cutoff_at:
                raise ValueError("Candidate publication exceeds discovery cutoff")
        # Pair validation limits subjects to 1000; validate bounded batches without dropping records.
        urls = sorted({raw.source_url for raw in result.records})
        if len(urls) > 1000:
            for offset in range(0, len(result.records), 1000):
                chunk = result.records[offset:offset + 1000]
                chunked = result.model_copy(update={"records": chunk,
                    "metadata": result.metadata.model_copy(update={"records_emitted": len(chunk)})})
                ExternalDiscoveryCollector.validate_observation_pair(request, chunked, job_count=job_count)
            return
        subjects = [CollectionSubject(repo_id=None, repo_full_name=None, package_id=None, url=url) for url in urls]
        projected_request = request.model_copy(update={"subjects": subjects or request.subjects,
                                                       "time_range": None, "cutoff_at": None,
                                                       "max_pages": min(TOTAL_REQUEST_LIMIT, request.max_pages * max(1, job_count))})
        projected_metadata = result.metadata.model_copy(update={
            "requested_time_range": None,
            "coverage": [coverage.model_copy(update={"time_range": None}) for coverage in result.metadata.coverage],
        })
        projected_result = result.model_copy(update={"metadata": projected_metadata})
        validate_collection_pair(projected_request, projected_result)

    def save_discovery_records(
        self,
        result: CollectionResult,
        target_path: Path | None = None,
        storage: JsonlStorage | None = None,
    ) -> Path:
        """Append missing immutable observations; one writer owns the JSONL file.

        Durable state may be replayed after publication or acknowledgement interruption.
        Existing IDs must have identical canonical contents, across every logical run.
        """
        store = storage or JsonlStorage()
        out_path = target_path or paths.raw_path("external", "discovery")
        persisted_by_id: dict[str, str] = {}
        pending: list[RawRecord] = []
        persisted = store.read_contract_records(out_path)
        for raw in [*persisted, *result.records]:
            raw = validate_record(raw)
            if not isinstance(raw, RawRecord):
                raise TypeError("Expected raw discovery observation")
            contents = _canonical_json(raw.model_dump(mode="json"))
            previous = persisted_by_id.get(raw.raw_record_id)
            if previous is not None and previous != contents:
                raise ValueError(f"Raw observation integrity conflict: {raw.raw_record_id}")
            persisted_by_id[raw.raw_record_id] = contents
        # Index once per publication; do not rewrite, reorder or remove any existing lines.
        existing_ids = {raw.raw_record_id for raw in persisted}
        for raw in result.records:
            if raw.raw_record_id not in existing_ids:
                pending.append(raw)
                existing_ids.add(raw.raw_record_id)
        if pending or not out_path.exists():
            store.append_contract_records_durable(out_path, pending)
        return out_path



__all__ = ["DiscoveryProvider", "ExternalDiscoveryCollector"]
