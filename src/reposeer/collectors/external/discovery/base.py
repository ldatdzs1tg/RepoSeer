"""External discovery collector orchestrating queries, pagination, and canonical envelopes."""

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from reposeer.clients.external.base import DiscoveryPageResult, DiscoveryProvider
from reposeer.collection.progress import CollectionProgress
from reposeer.contracts import (
    CollectionMetadata,
    CollectionRequest,
    CollectionResult,
    Coverage,
    DataError,
    RawRecord,
    RepoIdentity,
    validate_record,
)
from reposeer.contracts.identity import (
    canonical_article_url,
    format_utc_timestamp,
)
from reposeer.contracts.identity import (
    package_id as normalize_package_id,
)
from reposeer.contracts.models import MissingReason
from reposeer.exceptions import HTTPClientError, RateLimitExceededError
from reposeer.processing.normalize.timestamps import normalize_datetime
from reposeer.schemas.external.candidate import (
    ExternalCandidate,
    SubjectMatchStatus,
    evaluate_subject_match,
)
from reposeer.storage.paths import paths
from reposeer.storage.raw.jsonl import JsonlStorage
from reposeer.technology.models import SearchPlan

_STATUS_RANK: dict[SubjectMatchStatus, int] = {
    "exact_url_match": 4,
    "high_confidence_metadata": 3,
    "ambiguous_requires_crawl": 2,
    "function_or_alternative_only": 1,
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
    except Exception:
        return None


def _canonical_repo_id(val: str | None) -> str | None:
    if not val or not isinstance(val, str):
        return None
    val_clean = val.strip()
    if val_clean.startswith("github:repository:"):
        return val_clean
    return None


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
    ) -> tuple[CollectionResult, dict[str, Any]]:
        """Execute planned search queries across configured discovery providers.

        Respects cutoff bounds, performs multi-query candidate upsert, and emits
        canonical RawRecord envelopes with coverage manifest audit details.
        """
        request = validate_record(request)
        if not isinstance(request, CollectionRequest):
            raise TypeError("Expected CollectionRequest")

        started_at = self.clock()
        started_at_str = format_utc_timestamp(started_at)

        cutoff_dt: datetime | None = None
        if request.cutoff_at is not None:
            cutoff_dt = normalize_datetime(request.cutoff_at)

        window_start: datetime | None = None
        window_end: datetime | None = None
        if request.time_range is not None:
            window_start = normalize_datetime(request.time_range.start_at)
            window_end = normalize_datetime(request.time_range.end_at)

        effective_end: datetime | None = None
        if window_end is not None and cutoff_dt is not None:
            effective_end = min(window_end, cutoff_dt)
        elif window_end is not None:
            effective_end = window_end
        elif cutoff_dt is not None:
            effective_end = cutoff_dt

        is_historical = (
            historical_mode
            or (cutoff_dt is not None and cutoff_dt < started_at)
            or (request.time_range is not None)
        )

        canon_pkg_id = _canonical_package_id(package_id or search_plan.package_id)
        canon_repo_id = (
            repo_identity.repo_id
            if repo_identity
            else _canonical_repo_id(search_plan.repo_id)
        )
        repo_full_name = (
            repo_identity.repo_full_name
            if repo_identity
            else search_plan.repo_full_name
        )
        repo_url = str(repo_identity.repo_url) if repo_identity else None
        known_urls = [str(u) for u in repo_identity.known_urls] if repo_identity else None
        pkg_name = (
            canon_pkg_id.split("pypi:", 1)[1]
            if canon_pkg_id and canon_pkg_id.startswith("pypi:")
            else None
        )

        unique_candidates: dict[tuple[str, str], ExternalCandidate] = {}
        errors: list[DataError] = []
        per_query_stats: list[dict[str, Any]] = []
        pages_fetched = 0
        rejected = 0
        pagination_complete = True
        next_cursor: str | None = None
        historical_unavailable_encountered = False

        active_cursor = request.cursor

        for term in search_plan.terms:
            if term.platform in self.providers:
                targets = [(term.platform, self.providers[term.platform])]
            elif term.platform in ("all", "web"):
                targets = list(self.providers.items())
            else:
                targets = []

            if not targets:
                per_query_stats.append({
                    "query": term.query,
                    "query_id": term.query_id,
                    "intent": term.intent,
                    "platform": term.platform,
                    "provider": None,
                    "status": "no_provider",
                    "pages_fetched": 0,
                    "candidates_found": 0,
                    "has_more": False,
                    "missing_reason": "not_in_source",
                })
                continue

            for provider_name, provider in targets:
                if is_historical and not provider.supports_historical_window:
                    historical_unavailable_encountered = True
                    errors.append(
                        DataError(
                            code="insufficient_history",
                            message=f"Provider '{provider_name}' does not support historical window filtering",
                            retryable=False,
                            source_record_id=None,
                            raw_record_id=None,
                        )
                    )
                    per_query_stats.append({
                        "query": term.query,
                        "query_id": term.query_id,
                        "intent": term.intent,
                        "platform": term.platform,
                        "provider": provider_name,
                        "status": "unavailable",
                        "pages_fetched": 0,
                        "candidates_found": 0,
                        "has_more": False,
                        "missing_reason": "historical_unavailable",
                    })
                    continue

                query_pages = 0
                query_candidates = 0
                query_has_more = True
                current_cursor = active_cursor
                active_cursor = None

                while (
                    pages_fetched < request.max_pages
                    and len(unique_candidates) < request.max_records
                    and query_has_more
                ):
                    checkpoint_key = (
                        f"external.{provider_name}.{term.query_id or term.query}.p{current_cursor or '1'}"
                    )
                    if self.progress is not None and self.progress.context.get_checkpoint(checkpoint_key):
                        pass

                    try:
                        remaining_records = request.max_records - len(unique_candidates)
                        page_size = min(50, max(1, remaining_records))
                        page_result: DiscoveryPageResult = provider.search_page(
                            term.query,
                            window_start=window_start,
                            window_end=effective_end,
                            cursor=current_cursor,
                            page_size=page_size,
                        )
                    except RateLimitExceededError as err:
                        errors.append(
                            DataError(
                                code="rate_limited",
                                message=f"Rate limit exceeded on {provider_name}: {err}",
                                retryable=True,
                                source_record_id=None,
                                raw_record_id=None,
                            )
                        )
                        pagination_complete = False
                        next_cursor = current_cursor or "1"
                        query_has_more = True
                        if self.progress is not None:
                            self.progress.record_error()
                        break
                    except HTTPClientError as err:
                        is_rl = (
                            err.status_code == 429
                            or "rate" in str(err).lower()
                            or "throttle" in str(err).lower()
                        )
                        code = "rate_limited" if is_rl else "http_error"
                        errors.append(
                            DataError(
                                code=code,
                                message=f"HTTP error on {provider_name}: {err}",
                                retryable=err.status_code in (429, 500, 502, 503, 504),
                                source_record_id=None,
                                raw_record_id=None,
                            )
                        )
                        pagination_complete = False
                        next_cursor = current_cursor or "1"
                        query_has_more = True
                        if self.progress is not None:
                            self.progress.record_error()
                        break
                    except Exception as err:
                        errors.append(
                            DataError(
                                code="network_error",
                                message=f"Client/network error on {provider_name}: {err}",
                                retryable=True,
                                source_record_id=None,
                                raw_record_id=None,
                            )
                        )
                        pagination_complete = False
                        next_cursor = current_cursor or "1"
                        query_has_more = True
                        if self.progress is not None:
                            self.progress.record_error()
                        break

                    pages_fetched += 1
                    query_pages += 1
                    query_has_more = page_result.has_more
                    current_cursor = page_result.next_cursor

                    if self.progress is not None:
                        self.progress.record_success(
                            f"external.{provider_name}.{term.query_id or term.query}",
                            current_cursor or str(page_result.page_number),
                            count=len(page_result.candidates),
                        )

                    for candidate in page_result.candidates:
                        try:
                            canon_url = canonical_article_url(candidate.url)
                        except Exception as err:
                            rejected += 1
                            errors.append(
                                DataError(
                                    code="validation_error",
                                    message=f"Candidate URL validation failed: {candidate.url} ({err})",
                                    retryable=False,
                                    source_record_id=candidate.source_item_id,
                                    raw_record_id=None,
                                )
                            )
                            continue

                        if candidate.publication_date is not None:
                            cand_pub = normalize_datetime(candidate.publication_date)
                            if effective_end is not None and cand_pub > effective_end:
                                rejected += 1
                                errors.append(
                                    DataError(
                                        code="validation_error",
                                        message=f"Candidate publication date {cand_pub.isoformat()} is after cutoff/window_end {effective_end.isoformat()}",
                                        retryable=False,
                                        source_record_id=candidate.source_item_id,
                                        raw_record_id=None,
                                    )
                                )
                                continue
                            if window_start is not None and cand_pub <= window_start:
                                rejected += 1
                                errors.append(
                                    DataError(
                                        code="validation_error",
                                        message=f"Candidate publication date {cand_pub.isoformat()} is before or at window_start {window_start.isoformat()}",
                                        retryable=False,
                                        source_record_id=candidate.source_item_id,
                                        raw_record_id=None,
                                    )
                                )
                                continue
                        elif request.time_range is not None:
                            rejected += 1
                            errors.append(
                                DataError(
                                    code="validation_error",
                                    message="Candidate missing publication_date for historical window collection",
                                    retryable=False,
                                    source_record_id=candidate.source_item_id,
                                    raw_record_id=None,
                                )
                            )
                            continue

                        dedup_key = (candidate.source, candidate.source_item_id or canon_url)
                        match_status = evaluate_subject_match(
                            candidate,
                            query_intent=term.intent,
                            context_level=term.context_level,
                            repo_full_name=repo_full_name,
                            repo_url=repo_url,
                            package_name=pkg_name,
                            primary_keywords=term.keywords,
                            known_urls=known_urls,
                        )

                        if dedup_key not in unique_candidates:
                            candidate.query = term.query
                            candidate.query_id = term.query_id
                            candidate.query_intent = term.intent
                            candidate.context_level = term.context_level
                            candidate.subject_match_status = match_status
                            candidate.matched_queries = [term.query]
                            unique_candidates[dedup_key] = candidate
                            query_candidates += 1
                        else:
                            existing = unique_candidates[dedup_key]
                            if term.query not in existing.matched_queries:
                                existing.matched_queries.append(term.query)
                            if _STATUS_RANK.get(match_status, 0) > _STATUS_RANK.get(
                                existing.subject_match_status, 0
                            ):
                                existing.subject_match_status = match_status
                                existing.query = term.query
                                existing.query_id = term.query_id
                                existing.query_intent = term.intent
                                existing.context_level = term.context_level
                            if existing.score is None and candidate.score is not None:
                                existing.score = candidate.score
                            if existing.snippet is None and candidate.snippet is not None:
                                existing.snippet = candidate.snippet
                            if existing.author is None and candidate.author is not None:
                                existing.author = candidate.author
                            if existing.comment_count is None and candidate.comment_count is not None:
                                existing.comment_count = candidate.comment_count

                    if query_has_more and (
                        pages_fetched >= request.max_pages
                        or len(unique_candidates) >= request.max_records
                    ):
                        pagination_complete = False
                        next_cursor = current_cursor or str(pages_fetched + 1)
                        break

                per_query_stats.append({
                    "query": term.query,
                    "query_id": term.query_id,
                    "intent": term.intent,
                    "platform": term.platform,
                    "provider": provider_name,
                    "status": "ok" if not errors else "partial",
                    "pages_fetched": query_pages,
                    "candidates_found": query_candidates,
                    "has_more": query_has_more,
                    "missing_reason": None if not query_has_more else "not_collected",
                })

        now = self.clock()
        retrieved_at_str = format_utc_timestamp(now)
        records: list[RawRecord] = []

        for dedup_key in sorted(unique_candidates.keys()):
            cand = unique_candidates[dedup_key]
            canon_url = canonical_article_url(cand.url)

            event_at_str = None
            if cand.publication_date is not None:
                cand_pub = normalize_datetime(cand.publication_date)
                if cand_pub is not None:
                    clamped_pub = min(cand_pub, now)
                    event_at_str = format_utc_timestamp(clamped_pub)

            payload_dict = cand.model_dump(mode="json")
            payload_dict.update({
                "query_id": cand.query_id,
                "query_intent": cand.query_intent,
                "context_level": cand.context_level,
                "query_version": search_plan.query_version,
                "query_set_hash": search_plan.query_set_hash,
                "matched_queries": cand.matched_queries,
                "subject_match_status": cand.subject_match_status,
                "discovery_provider": cand.source,
                "window_start": format_utc_timestamp(window_start) if window_start else None,
                "window_end": format_utc_timestamp(window_end) if window_end else None,
                "cutoff_at": request.cutoff_at,
                "record_stage": "discovery_candidate",
                "discovery_status": "unverified",
                "content_status": "not_fetched",
            })

            canonical_payload_bytes = json.dumps(payload_dict, sort_keys=True, ensure_ascii=True).encode(
                "utf-8"
            )
            raw_body_sha256 = hashlib.sha256(canonical_payload_bytes).hexdigest()

            source_record_id = (
                cand.source_item_id.strip()
                if cand.source_item_id and cand.source_item_id.strip()
                else None
            )

            raw = RawRecord(
                schema_version="0.1.0",
                record_type="raw_record",
                raw_record_id=f"raw:{uuid4()}",
                run_id=request.run_id,
                source="web_article",
                source_record_type="article",
                source_record_id=source_record_id,
                repo_id=canon_repo_id,
                package_id=canon_pkg_id,
                source_url=canon_url,
                retrieved_at=retrieved_at_str,
                event_at=event_at_str,
                available_at=None,
                http_status=200,
                raw_body_sha256=raw_body_sha256,
                payload=payload_dict,
            )
            validated_raw = validate_record(raw)
            if not isinstance(validated_raw, RawRecord):
                raise TypeError("Expected RawRecord")
            records.append(validated_raw)

        pub_dates = [
            c.publication_date
            for c in unique_candidates.values()
            if c.publication_date is not None
        ]
        observed_from = min(pub_dates) if pub_dates else None
        observed_to = max(pub_dates) if pub_dates else None

        if historical_unavailable_encountered and not records:
            coverage_status = "unavailable"
            coverage_reason: MissingReason | None = "historical_unavailable"
        elif errors and not records:
            coverage_status = "unavailable"
            coverage_reason = "unavailable"
        elif not pagination_complete:
            coverage_status = "partial"
            coverage_reason = "not_collected"
        elif rejected > 0 or errors or historical_unavailable_encountered:
            coverage_status = "partial"
            coverage_reason = (
                "historical_unavailable"
                if historical_unavailable_encountered
                else "not_collected"
            )
        else:
            coverage_status = "complete"
            coverage_reason = None

        if coverage_status == "complete" and pagination_complete and not errors and rejected == 0:
            result_status = "complete"
        elif not records and errors:
            result_status = "failed"
        else:
            result_status = "partial"

        metadata = CollectionMetadata(
            collector_name=request.collector_name,
            collector_version="0.1.0",
            started_at=started_at_str,
            finished_at=format_utc_timestamp(self.clock()),
            requested_time_range=request.time_range,
            pages_fetched=pages_fetched,
            records_emitted=len(records),
            records_rejected=rejected,
            pagination_complete=pagination_complete,
            next_cursor=next_cursor if not pagination_complete else None,
            coverage=[
                Coverage(
                    data_group="articles",
                    status=coverage_status,
                    time_range=request.time_range,
                    records_observed=len(records) if coverage_status != "unavailable" else 0,
                    missing_reason=coverage_reason,
                )
            ],
            rate_limit_remaining=None,
            rate_limit_reset_at=None,
        )

        result = CollectionResult(
            schema_version="0.1.0",
            record_type="collection_result",
            request_id=request.request_id,
            run_id=request.run_id,
            source=request.source,
            status=result_status,
            records=records,
            errors=errors,
            metadata=metadata,
        )
        validated_result = validate_record(result)
        if not isinstance(validated_result, CollectionResult):
            raise TypeError("Expected CollectionResult")

        discovery_manifest = {
            "query_set_hash": search_plan.query_set_hash,
            "query_version": search_plan.query_version,
            "profile_status": search_plan.profile_status,
            "package_id": canon_pkg_id,
            "repo_id": canon_repo_id,
            "per_query_stats": per_query_stats,
            "observed_from": format_utc_timestamp(observed_from) if observed_from else None,
            "observed_to": format_utc_timestamp(observed_to) if observed_to else None,
            "pagination_complete": pagination_complete,
            "completeness_status": coverage_status,
            "records_observed": len(records) if coverage_status != "unavailable" else 0,
            "records_rejected": rejected,
        }

        return validated_result, discovery_manifest

    def save_discovery_records(
        self,
        result: CollectionResult,
        target_path: Path | None = None,
        storage: JsonlStorage | None = None,
    ) -> Path:
        """Append candidate raw records to JSONL storage."""
        store = storage or JsonlStorage()
        out_path = target_path or paths.raw_path("external", "discovery")
        for record in result.records:
            store.append_contract_record(out_path, record)
        return out_path


__all__ = ["DiscoveryProvider", "ExternalDiscoveryCollector"]
