"""GitHub repository contract boundary using the existing T-012 provider."""

from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import ValidationError

from reposeer.clients.github import GitHubClient
from reposeer.collection.errors import handle_collection_error
from reposeer.contracts.identity import canonical_github_url, format_utc_timestamp, repo_id
from reposeer.contracts.models import (
    SCHEMA_VERSION,
    CollectionMetadata,
    CollectionRequest,
    CollectionResult,
    Coverage,
    DataError,
    GitHubRepository,
    MissingField,
    NormalizationRequest,
    NormalizationResult,
    Provenance,
    RawRecord,
    RepoIdentity,
)
from reposeer.contracts.validation import (
    validate_collection_pair,
    validate_normalization_pair,
    validate_record,
)
from reposeer.exceptions import HTTPClientError, RateLimitExceededError


def _now() -> datetime:
    return datetime.now(UTC)


def _error(
    code: str, message: str, *, raw: RawRecord | None = None, retryable: bool = False
) -> DataError:
    return DataError(
        code=code,
        message=message,
        retryable=retryable,
        source_record_id=raw.source_record_id if raw else None,
        raw_record_id=raw.raw_record_id if raw else None,
    )


def _native_id(payload: dict) -> str:
    value = payload.get("id")
    if type(value) is not int or value <= 0:
        raise ValueError("native GitHub ID must be a positive integer")
    return str(value)


def _subject_name(subject) -> str:
    name = subject.repo_full_name
    if subject.url is not None:
        parts = urlsplit(subject.url)
        if (
            parts.hostname.lower() != "github.com"
            or parts.query
            or parts.fragment
            or parts.port is not None
        ):
            raise ValueError("subject URL must identify a GitHub repository")
        url_name = parts.path.strip("/")
        canonical_github_url(url_name)
        if name is not None and name.casefold() != url_name.casefold():
            raise ValueError("subject name/URL mismatch")
        name = url_name
    if name is None:
        raise ValueError("repository name or GitHub URL is required")
    canonical_github_url(name)
    return name


class GitHubRepositoryContractCollector:
    def __init__(self, client: GitHubClient | Callable | None = None, *, clock: Callable = _now):
        self.client = client if client is not None else GitHubClient()
        self._owns_client = client is None
        self.clock = clock

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def collect(self, request: CollectionRequest) -> CollectionResult:
        request = validate_record(request)
        if not isinstance(request, CollectionRequest):
            raise TypeError("expected CollectionRequest")
        started = format_utc_timestamp(self.clock())
        records, errors = [], []
        fetched = 0
        reason = "unavailable"
        if (
            request.source != "github_rest"
            or request.record_kinds != ["github_repository"]
            or len(request.subjects) != 1
        ):
            errors.append(
                _error(
                    "unsupported_record_type",
                    "Only one github_rest repository subject is supported",
                )
            )
        elif request.time_range is not None or request.cutoff_at is not None:
            reason = "historical_unavailable"
            errors.append(
                _error(
                    "insufficient_history",
                    "Current repository observations cannot supply historical coverage",
                )
            )
        elif request.cursor is not None:
            errors.append(
                _error("unsupported_record_type", "Repository observations do not support cursors")
            )
        else:
            subject = request.subjects[0]
            if (
                subject.package_id is not None
                and subject.repo_full_name is None
                and subject.url is None
            ):
                errors.append(
                    _error("not_mapped", "No verified package-to-repository mapping supplied")
                )
            else:
                try:
                    name = _subject_name(subject)
                    owner, repo = name.split("/")
                    fetch = self.client if callable(self.client) else self.client.get_repository
                    payload = fetch(owner, repo)
                    fetched = 1
                    if not isinstance(payload, dict):
                        raise ValueError("native repository payload must be an object")
                    numeric_id = _native_id(payload)
                    canonical_id = repo_id(numeric_id)
                    if subject.repo_id is not None and subject.repo_id != canonical_id:
                        raise ValueError("requested repository ID does not match provider ID")
                    base_url = getattr(self.client, "base_url", "https://api.github.com")
                    records.append(
                        RawRecord(
                            schema_version=SCHEMA_VERSION,
                            record_type="raw_record",
                            raw_record_id="raw:" + str(uuid4()),
                            run_id=request.run_id,
                            source="github_rest",
                            source_record_type="github_repository",
                            source_record_id=numeric_id,
                            repo_id=canonical_id,
                            package_id=None,
                            source_url=f"{base_url.rstrip('/')}/repos/{owner}/{repo}",
                            retrieved_at=format_utc_timestamp(self.clock()),
                            event_at=None,
                            available_at=None,
                            http_status=None,
                            raw_body_sha256=None,
                            payload=payload,
                        )
                    )
                except HTTPClientError as error:
                    if isinstance(error, RateLimitExceededError):
                        code = "rate_limited"
                    elif error.status_code == 404:
                        code = "not_found"
                    elif error.status_code is None:
                        code = "network_error"
                    elif error.status_code == 200:
                        code = "parse_error"
                    else:
                        code = "http_error"
                    errors.append(
                        _error(
                            code,
                            "GitHub repository request failed",
                            retryable=code in {"rate_limited", "network_error"}
                            or error.status_code in {500, 502, 503, 504},
                        )
                    )
                except (ValueError, TypeError) as error:
                    code = (
                        "validation_error"
                        if isinstance(error, ValidationError) or fetched
                        else "parse_error"
                    )
                    errors.append(
                        _error(code, "Invalid repository subject or native repository payload")
                    )
        if errors:
            # Log sanitized diagnostics; provider response bodies/validation inputs may contain secrets.
            handle_collection_error(ValueError(errors[0].message), "github_repository_contract")
        result = CollectionResult(
            schema_version=SCHEMA_VERSION,
            record_type="collection_result",
            request_id=request.request_id,
            run_id=request.run_id,
            source=request.source,
            status="complete" if records else "failed",
            records=records,
            errors=errors,
            metadata=CollectionMetadata(
                collector_name=request.collector_name,
                collector_version=SCHEMA_VERSION,
                started_at=started,
                finished_at=format_utc_timestamp(self.clock()),
                requested_time_range=request.time_range,
                pages_fetched=fetched,
                records_emitted=len(records),
                records_rejected=1 if fetched and errors else 0,
                pagination_complete=bool(records),
                next_cursor=None,
                coverage=[
                    Coverage(
                        data_group="repo_identity",
                        status="complete" if records else "unavailable",
                        time_range=request.time_range,
                        records_observed=len(records),
                        missing_reason=None if records else reason,
                    )
                ],
                rate_limit_remaining=None,
                rate_limit_reset_at=None,
            ),
        )
        validate_collection_pair(request, result)
        return result


def _provider_timestamp(value) -> str:
    if not isinstance(value, str):
        raise ValueError("provider timestamp must be an aware timestamp string")
    return format_utc_timestamp(datetime.fromisoformat(value))


class GitHubRepositoryContractNormalizer:
    def normalize(self, request: NormalizationRequest) -> NormalizationResult:
        request = validate_record(request)
        if not isinstance(request, NormalizationRequest):
            raise TypeError("expected NormalizationRequest")
        records, errors = [], []
        for raw in request.records:
            if raw.source_record_type != "github_repository" or raw.source != "github_rest":
                errors.append(
                    _error(
                        "unsupported_record_type",
                        "No normalizer for this source/record kind",
                        raw=raw,
                    )
                )
                continue
            try:
                records.append(self._normalize(raw))
            except (ValueError, TypeError, KeyError):
                errors.append(
                    _error(
                        "validation_error",
                        "Native payload cannot satisfy the canonical repository contract",
                        raw=raw,
                    )
                )
        result = NormalizationResult(
            schema_version=SCHEMA_VERSION,
            record_type="normalization_result",
            request_id=request.request_id,
            run_id=request.run_id,
            normalizer_version=SCHEMA_VERSION,
            status="partial" if records and errors else "failed" if errors else "complete",
            input_record_count=len(request.records),
            processed_record_count=len(request.records) - len(errors),
            rejected_record_count=len(errors),
            output_record_count=len(records),
            records=records,
            errors=errors,
        )
        validate_normalization_pair(request, result)
        return result

    @staticmethod
    def _normalize(raw: RawRecord) -> GitHubRepository:
        payload = raw.payload
        numeric_id = _native_id(payload)
        if raw.repo_id != repo_id(numeric_id) or raw.source_record_id != numeric_id:
            raise ValueError("raw identity does not match native ID")
        missing = []

        def optional(native: str, canonical: str):
            if native not in payload or payload[native] is None:
                missing.append(
                    MissingField(field_path=canonical, reason="not_in_source", detail=None)
                )
            return payload.get(native)

        identity = RepoIdentity(
            repo_id=raw.repo_id,
            github_repo_id=numeric_id,
            github_node_id=optional("node_id", "identity.github_node_id"),
            repo_full_name=payload["full_name"],
            repo_url=canonical_github_url(payload["full_name"]),
            known_urls=[],
        )
        values = {
            canonical: optional(native, canonical)
            for native, canonical in {
                "description": "description",
                "mirror_url": "mirror_url",
                "language": "primary_language",
                "stargazers_count": "stargazers_count",
                "forks_count": "forks_count",
                "open_issues_count": "open_issues_and_prs_count",
            }.items()
        }
        pushed = optional("pushed_at", "pushed_at")
        values["pushed_at"] = _provider_timestamp(pushed) if pushed is not None else None
        values["language_bytes"] = None
        missing.append(
            MissingField(field_path="language_bytes", reason="not_collected", detail=None)
        )
        visibility = payload.get("visibility")
        if visibility is None and type(payload.get("private")) is bool:
            visibility = "private" if payload["private"] else "public"
        return GitHubRepository(
            schema_version=SCHEMA_VERSION,
            record_type="github_repository",
            identity=identity,
            visibility=visibility,
            is_fork=payload.get("fork"),
            is_template=payload.get("is_template"),
            is_archived=payload.get("archived"),
            created_at=_provider_timestamp(payload["created_at"]),
            updated_at=_provider_timestamp(payload["updated_at"]),
            **values,
            provenance=Provenance(
                source=raw.source,
                source_url=raw.source_url,
                retrieved_at=raw.retrieved_at,
                valid_at=raw.retrieved_at,
                available_at=raw.retrieved_at,
                temporal_basis="observation",
                raw_record_ids=[raw.raw_record_id],
                missing_fields=missing,
            ),
        )
