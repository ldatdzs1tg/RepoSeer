"""T-017 current external documents: native API/HTML capture, extraction and audit."""

import base64
import hashlib
import html
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import trafilatura

from reposeer.collection.http.client import HTTPClient
from reposeer.collectors.external.crawler.extractor import ArticleExtractor
from reposeer.collectors.external.crawler.fetcher import HTMLFetcher
from reposeer.collectors.external.crawler.metadata import publication
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.collectors.external.crawler.scope import RepositoryScopeEvidence
from reposeer.collectors.external.crawler.validation import validate_external_document_record
from reposeer.contracts import (
    CollectionMetadata,
    CollectionRequest,
    CollectionResult,
    Coverage,
    DataError,
    RawRecord,
    validate_collection_pair,
    validate_record,
)
from reposeer.contracts.identity import (
    canonical_article_url,
    format_utc_timestamp,
    normalize_article_text,
    text_sha256,
)
from reposeer.exceptions import ExtractionError, HTTPClientError, RateLimitExceededError
from reposeer.schemas.external.candidate import ExternalCandidate

CandidateInput = RawRecord | ExternalCandidate | str
CRAWLER_VERSION = "0.3.0"
EXTRACTION_REASONS = {
    "empty_response",
    "unsupported_content_type",
    "invalid_native_json",
    "empty_content",
    "parser_error",
    "native_item_unavailable",
    "native_identity_mismatch",
    "native_url_mismatch",
    "native_body_unavailable",
    "native_parser_error",
    "invalid_extracted_document",
}



@dataclass
class CandidateGroup:
    """One primary fetch and compact references to every contributing observation."""

    primary: CandidateInput
    references: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)


def discovery_reference(data: dict[str, Any]) -> dict[str, Any]:
    snapshot = data.get("discovery") or {}
    payload = snapshot.get("payload", snapshot)
    values = {
        "candidate_raw_record_id": data.get("candidate_raw_record_id"),
        "candidate_id": data.get("candidate_id"),
        "discovery_run_id": snapshot.get("run_id"),
        "discovery_provider": data.get("discovery_provider"),
        "discovery_source_item_id": data.get("discovery_source_item_id"),
        "discovery_source_url": data.get("discovery_source_url"),
    }
    for name in ("query_id", "query_intent", "query_associations", "query_set_hash",
                 "query_version", "context_level", "raw_record_ids", "window_start", "window_end", "cutoff_at",
                 "search_target_package_id", "search_target_repo_id", "search_target_repo_full_name",
                 "subject_match_status", "discovered_at", "published_at"):
        if name in payload:
            values[name] = payload[name]
    return {name: value for name, value in values.items() if value is not None}


def candidate_data(value: CandidateInput | CandidateGroup) -> dict[str, Any]:
    """Retain discovery lineage verbatim; association is not verified relevance."""
    if isinstance(value, CandidateGroup):
        data = candidate_data(value.primary)
        references = sorted(value.references.values(), key=lambda ref: json.dumps(ref, sort_keys=True))
        data["discovery_references"] = references
        data["discovery_lineage_complete"] = bool(references) and all(
            ref.get("projection_verified") is True for ref in references
        )
        return data
    if isinstance(value, RawRecord):
        value = validate_record(value)
        if (
            value.source != "web_article"
            or value.source_record_type != "article"
            or value.payload.get("record_stage") != "discovery_candidate"
        ):
            raise ValueError("expected_discovery_candidate")
        candidate_url = value.payload.get("candidate_url")
        if not isinstance(candidate_url, str):
            raise ValueError("discovery_candidate_url_required")
        candidate_url = canonical_article_url(candidate_url)
        return {
            "url": candidate_url,
            "candidate_id": value.payload.get("candidate_id"),
            "source": "web",
            "source_item_id": None,
            "discovery_provider": value.payload.get("discovery_provider") or value.payload.get("source"),
            "discovery_source_item_id": value.source_record_id,
            "discovery_source_url": value.source_url,
            "title": value.payload.get("title"),
            "search_association": {key: value.payload.get("search_target_" + key)
                                   for key in ("package_id", "repo_id", "repo_full_name")}
                                  if value.payload.get("search_target_package_id") else None,
            "repo_id": value.repo_id,
            "package_id": value.package_id,
            "candidate_raw_record_id": value.raw_record_id,
            "discovery": value.model_dump(mode="json"),
        }
    if isinstance(value, ExternalCandidate):
        data = ExternalCandidate.model_validate(value.model_dump()).model_dump(mode="json")
        return {**data, "discovery": dict(data), "source": "web", "source_item_id": None,
                "discovery_provider": data["source"], "discovery_source_item_id": data["source_item_id"],
                "discovery_source_url": data.get("source_item_url")}
    if isinstance(value, str):
        # This validates syntax, not identity, DNS or package relevance.
        canonical_article_url(value)
        return {"url": value, "source": "web", "source_item_id": None, "discovery": None}
    raise TypeError("expected_candidate_record_or_url")


def native_endpoint(data: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Route only recognized native URLs; an HN-linked blog stays a web fetch."""
    url = data["url"]
    parts = urlsplit(url)
    host = parts.hostname.lower()
    provider = data.get("discovery_provider") or data.get("source")
    source_id = data.get("discovery_source_item_id") or data.get("source_item_id") or ""
    if host == "news.ycombinator.com" and parts.path == "/item":
        item_id = parse_qs(parts.query).get("id", [""])[0]
        if not re.fullmatch(r"[1-9][0-9]*", item_id):
            raise ValueError("invalid_hn_item_id")
        if provider == "hackernews" and source_id and source_id not in {item_id, f"hn:{item_id}"}:
            raise ValueError("native_identity_mismatch")
        return f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json", "hackernews", item_id
    if host == "dev.to" and provider == "devto":
        match = re.fullmatch(r"(?:devto:)?([1-9][0-9]*)", source_id)
        if match:
            return f"https://dev.to/api/articles/{match[1]}", "devto", match[1]
    if host == "stackoverflow.com" or host.endswith(".stackexchange.com"):
        match = re.match(r"^/questions/([1-9][0-9]*)(?:/|$)", parts.path)
        if match:
            item_id = match[1]
            if provider == "stackexchange" and source_id and source_id not in {item_id, f"se:{item_id}"}:
                raise ValueError("native_identity_mismatch")
            site = "stackoverflow" if host == "stackoverflow.com" else host.split(".")[0]
            return (
                f"https://api.stackexchange.com/2.3/questions/{item_id}?site={site}&filter=withbody",
                "stackexchange",
                item_id,
            )
    return url, None, None


class ExternalCrawlerCollector:
    """Collect a bounded candidate batch into canonical raw envelopes.

    Only current observations are supported. Historical cutoffs from discovery
    remain lineage, never proof of the availability of freshly fetched text.
    """

    def __init__(
        self,
        http_client: HTTPClient,
        *,
        policy: URLPolicy | None = None,
        extractor: ArticleExtractor | None = None,
        clock: Callable[[], datetime] | None = None,
        max_redirects: int = 5,
        max_response_bytes: int = 5_000_000,
        repository_evidence: Sequence[RepositoryScopeEvidence] = (),
    ):
        if type(max_redirects) is not int or not 0 <= max_redirects <= 20:
            raise ValueError("max_redirects must be between 0 and 20")
        if type(max_response_bytes) is not int or max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        self.fetcher = HTMLFetcher(http_client)
        self.policy = policy or URLPolicy()
        self.extractor = extractor or ArticleExtractor()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_redirects = max_redirects
        self.max_response_bytes = max_response_bytes
        self.repository_scopes = {}
        for evidence in repository_evidence:
            scope = evidence.resolve()
            package = scope["package_id"]
            if (
                package in self.repository_scopes
                and self.repository_scopes[package]["repo_id"] != scope["repo_id"]
            ):
                raise ValueError("conflicting_repository_scope")
            self.repository_scopes[package] = scope

    def collect(
        self, request: CollectionRequest, candidates: Sequence[CandidateInput | CandidateGroup]
    ) -> CollectionResult:
        request = validate_record(request)
        if not isinstance(request, CollectionRequest):
            raise TypeError("expected_collection_request")
        if request.source != "web_article" or request.record_kinds != ["article"]:
            raise ValueError("crawler_requires_web_articles")
        if request.cutoff_at is not None or request.time_range is not None:
            raise ValueError("historical_content_reconstruction_unsupported")
        offset = int(request.cursor) if request.cursor and request.cursor.isdecimal() else 0
        if request.cursor is not None and (
            not request.cursor.isdecimal() or offset > len(candidates)
        ):
            raise ValueError("invalid_candidate_cursor")
        started = format_utc_timestamp(self.clock())
        records: list[RawRecord] = []
        errors: list[DataError] = []
        rejected = 0
        end = min(len(candidates), offset + min(request.max_pages, request.max_records))
        for candidate in candidates[offset:end]:
            data = candidate_data(candidate)
            raw = None
            try:
                association = data.get("search_association")
                scope = self.repository_scopes.get(
                    association.get("package_id") if association else data.get("package_id"))
                if association and scope:
                    full_name = scope["repository_url"].removeprefix("https://github.com/")
                    if (association.get("repo_id") != scope["repo_id"]
                            or str(association.get("repo_full_name", "")).casefold() != full_name.casefold()):
                        scope = None
                if scope:
                    if data.get("repo_id") not in {None, scope["repo_id"]}:
                        raise ValueError("candidate_repository_scope_mismatch")
                    if scope["observed_at"] > format_utc_timestamp(self.clock()):
                        raise ValueError("repository_scope_not_yet_observed")
                    data["repo_id"] = scope["repo_id"]
                    data["package_id"] = scope["package_id"]
                # Check the candidate too, even when fetching a fixed native endpoint.
                self.policy.validate(data["url"])
                endpoint, native, item_id = native_endpoint(data)
                data["source"] = native or "web"
                if native:
                    prefix = {"hackernews": "hn", "devto": "devto", "stackexchange": "se"}[native]
                    data["source_item_id"] = f"{prefix}:{item_id}"
                response = self.fetcher.fetch_response(
                    endpoint,
                    policy=self.policy,
                    max_redirects=self.max_redirects,
                    max_response_bytes=self.max_response_bytes,
                    accept="application/json" if native else "text/html,application/xhtml+xml",
                )
                fetched = self.clock()
                fetched_at = format_utc_timestamp(fetched)
                if not response.content.strip():
                    raise ExtractionError("empty_response")
                payload: dict[str, Any] = {
                    "record_stage": "external_document",
                    "crawler_version": CRAWLER_VERSION,
                    "source": data["source"],
                    "source_url": data["url"],
                    "url": data["url"] if native else str(response.url),
                    "fetched_url": str(response.url),
                    "fetched_at": fetched_at,
                    "source_item_id": data.get("source_item_id"),
                    "candidate_raw_record_id": data.get("candidate_raw_record_id"),
                    "discovery": data.get("discovery"),
                    "candidate_id": data.get("candidate_id"),
                    "discovery_provider": data.get("discovery_provider"),
                    "discovery_source_item_id": data.get("discovery_source_item_id"),
                    "discovery_source_url": data.get("discovery_source_url"),
                    "discovery_references": data.get("discovery_references", [discovery_reference(data)] if data.get("discovery") else []),
                    "discovery_lineage_complete": data.get("discovery_lineage_complete", False),
                    "response_body_base64": base64.b64encode(response.content).decode("ascii"),
                    "content_type": response.headers.get("content-type", "").strip() or None,
                    "etag": response.headers.get("etag", "").strip() or None,
                    "last_modified": response.headers.get("last-modified", "").strip() or None,
                    "native_payload": None,
                    "html": None,
                    "title": None,
                    "author": None,
                    "published_at": None,
                    "published_date": None,
                    "published_precision": "unknown",
                    "updated_at": None,
                    "content": None,
                    "content_status": "unavailable",
                    "content_sha256": None,
                    "word_count": None,
                    "content_available_at": None,
                    "historical_version_status": "unverified",
                    "relevance_status": "unverified",
                    "repository_scope": scope,
                    "availability_basis": "observed_response",
                }
                raw = RawRecord(
                    schema_version="0.1.0",
                    record_type="raw_record",
                    raw_record_id=f"raw:{uuid4()}",
                    run_id=request.run_id,
                    source="web_article",
                    source_record_type="article",
                    source_record_id=data.get("source_item_id"),
                    repo_id=data.get("repo_id"),
                    package_id=data.get("package_id"),
                    source_url=data["url"],
                    retrieved_at=fetched_at,
                    event_at=None,
                    available_at=fetched_at,
                    http_status=response.status_code,
                    raw_body_sha256=hashlib.sha256(response.content).hexdigest(),
                    payload=payload,
                )
                media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if native:
                    if media_type not in {"application/json", "text/json"}:
                        raise ExtractionError("unsupported_content_type")
                    try:
                        native_payload = response.json()
                        json.dumps(native_payload, allow_nan=False)
                    except (ValueError, RecursionError) as error:
                        raise ExtractionError("invalid_native_json") from error
                    payload["native_payload"] = native_payload
                    payload.update(
                        self._native_document(native, native_payload, item_id, data, fetched)
                    )
                else:
                    if media_type not in {"text/html", "application/xhtml+xml"}:
                        raise ExtractionError("unsupported_content_type")
                    payload["html"] = response.text
                    document = self.extractor.extract(
                        response.text,
                        str(response.url),
                        data["source"],
                        fetched_at=fetched,
                        strict=True,
                    )
                    if document is None:
                        raise ExtractionError("empty_content")
                    payload.update(
                        document.model_dump(mode="json", exclude={"extracted_at", "source"})
                    )
                    payload["published_at"] = (
                        format_utc_timestamp(document.published_at)
                        if document.published_at
                        else None
                    )
                    payload["updated_at"] = (
                        format_utc_timestamp(document.updated_at) if document.updated_at else None
                    )
                payload["content"] = normalize_article_text(payload["content"])
                payload["content_sha256"] = text_sha256(payload["content"])
                payload["word_count"] = len(payload["content"].split())
                payload["content_status"] = "full"
                # This captured revision is proven available now, not at publication.
                payload["content_available_at"] = fetched_at
                payload["historical_version_status"] = "observation_only"
                raw.event_at = payload["published_at"]
                raw.payload = payload
                try:
                    validate_external_document_record(raw)
                except ValueError as error:
                    raise ExtractionError("invalid_extracted_document") from error
            except (HTTPClientError, ValueError, ExtractionError) as error:
                if raw is not None:
                    # Keep source evidence, but never persist partially invalid extraction.
                    for key in (
                        "title",
                        "author",
                        "published_at",
                        "published_date",
                        "updated_at",
                        "content",
                        "content_sha256",
                        "word_count",
                        "content_available_at",
                    ):
                        payload[key] = None
                    payload.update(
                        content_status="unavailable",
                        published_precision="unknown",
                        historical_version_status="unverified",
                    )
                    raw.event_at = None
                rejected += 1
                if isinstance(error, RateLimitExceededError):
                    code, message, retryable = "rate_limited", "rate_limit_exhausted", True
                elif isinstance(error, HTTPClientError):
                    status = error.status_code
                    code = (
                        "network_error"
                        if status is None
                        else "not_found"
                        if status == 404
                        else "http_error"
                    )
                    message = f"fetch_failed:{status or 'network_or_size_limit'}"
                    retryable = status is None or status >= 500
                else:
                    code = (
                        "parse_error" if isinstance(error, ExtractionError) else "validation_error"
                    )
                    # Only controlled reason codes may cross the error boundary.
                    message, retryable = (
                        str(error)
                        if str(error) in EXTRACTION_REASONS
                        else "url_or_identity_rejected",
                        False,
                    )
                errors.append(
                    DataError(
                        code=code,
                        message=message,
                        retryable=retryable,
                        source_record_id=data.get("source_item_id"),
                        raw_record_id=raw.raw_record_id if raw else None,
                    )
                )
                if isinstance(candidate, CandidateGroup):
                    candidate.errors.append(errors[-1].model_dump(mode="json"))
            if raw is not None:
                # RawRecord construction copies payload; attach the completed capture explicitly.
                raw.payload = payload
                records.append(validate_external_document_record(raw))
        complete = end == len(candidates)
        status = (
            "failed"
            if errors and not records
            else "partial"
            if errors or not complete
            else "complete"
        )
        coverage_status = (
            "unavailable"
            if status == "failed"
            else "partial"
            if status == "partial"
            else "complete"
        )
        result = CollectionResult(
            schema_version="0.1.0",
            record_type="collection_result",
            request_id=request.request_id,
            run_id=request.run_id,
            source="web_article",
            status=status,
            records=records,
            errors=errors,
            metadata=CollectionMetadata(
                collector_name=request.collector_name,
                collector_version=CRAWLER_VERSION,
                started_at=started,
                finished_at=format_utc_timestamp(self.clock()),
                requested_time_range=None,
                pages_fetched=end - offset,
                records_emitted=len(records),
                records_rejected=rejected,
                pagination_complete=complete,
                next_cursor=str(end) if not complete else None,
                coverage=[
                    Coverage(
                        data_group="articles",
                        status=coverage_status,
                        time_range=None,
                        records_observed=len(records),
                        missing_reason=None
                        if coverage_status == "complete"
                        else "unavailable"
                        if coverage_status == "unavailable"
                        else "not_collected",
                    )
                ],
                rate_limit_remaining=None,
                rate_limit_reset_at=None,
            ),
        )
        validate_collection_pair(request, result)
        return result

    @staticmethod
    def _native_document(native, payload, item_id, data, fetched) -> dict[str, Any]:
        item = payload
        if native == "stackexchange":
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("items"), list)
                or len(payload["items"]) != 1
            ):
                raise ExtractionError("native_item_unavailable")
            item = payload["items"][0]
        if not isinstance(item, dict) or item.get("deleted") or item.get("dead"):
            raise ExtractionError("native_item_unavailable")
        id_key = "question_id" if native == "stackexchange" else "id"
        if str(item.get(id_key)) != item_id:
            raise ExtractionError("native_identity_mismatch")
        if (
            native == "devto"
            and item.get("url")
            and canonical_article_url(item["url"]) != canonical_article_url(data["url"])
        ):
            raise ExtractionError("native_url_mismatch")
        body = (
            item.get("text")
            if native == "hackernews"
            else item.get("body_html")
            if native == "devto"
            else item.get("body")
        )
        if not isinstance(body, str) or not body.strip():
            raise ExtractionError("native_body_unavailable")
        try:
            content = normalize_article_text(
                trafilatura.html2txt(f"<html><body>{body}</body></html>")
            )
        except Exception as error:
            raise ExtractionError("native_parser_error") from error
        if not content:
            raise ExtractionError("empty_content")
        pub = item.get("published_at") or item.get("published_timestamp")
        if native != "devto":
            epoch = item.get("time" if native == "hackernews" else "creation_date")
            try:
                pub = (
                    format_utc_timestamp(datetime.fromtimestamp(epoch, UTC))
                    if type(epoch) in {int, float}
                    else None
                )
            except (ValueError, OverflowError, OSError):
                pub = None
        published_at, published_date, precision = publication(pub, fetched)
        edited = item.get("edited_at")
        if native == "stackexchange" and type(item.get("last_edit_date")) in {int, float}:
            try:
                edited = format_utc_timestamp(datetime.fromtimestamp(item["last_edit_date"], UTC))
            except (ValueError, OverflowError, OSError):
                edited = None
        updated_at, _, _ = publication(edited, fetched)
        if updated_at and published_at and updated_at < published_at:
            updated_at = None
        owner = item.get("user" if native == "devto" else "owner")
        owner = owner if isinstance(owner, dict) else {}
        author = (
            item.get("by")
            if native == "hackernews"
            else owner.get("name" if native == "devto" else "display_name")
        )

        def metadata_text(value):
            if value is None:
                return None
            if not isinstance(value, str):
                raise ExtractionError("invalid_extracted_document")
            return html.unescape(value).strip() or None

        return {
            "title": metadata_text(item.get("title")),
            "author": metadata_text(author),
            "content": content,
            "published_at": published_at,
            "published_date": published_date,
            "published_precision": precision,
            "updated_at": updated_at,
        }
