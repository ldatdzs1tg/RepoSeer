"""Canonical repository collection, batch screening and append-only observations."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import polars as pl

from reposeer.contracts import (
    SCHEMA_VERSION,
    CollectionRequest,
    CollectionResult,
    CollectionSubject,
    GitHubRepository,
    NormalizationRequest,
    RawRecord,
    validate_collection_pair,
    validate_normalization_pair,
)
from reposeer.contracts.adapters import (
    GitHubRepositoryContractCollector,
    GitHubRepositoryContractNormalizer,
)
from reposeer.contracts.identity import canonical_github_url, validate_http_url
from reposeer.exceptions import CollectionError, StorageError
from reposeer.storage.paths import StoragePaths, paths
from reposeer.storage.processed.parquet import ParquetStorage
from reposeer.storage.raw.jsonl import JsonlStorage


@dataclass(frozen=True, slots=True)
class RepositoryBatchItem:
    target: str
    status: Literal["stored", "excluded", "duplicate", "failed"]
    repo_id: str | None
    screening_reasons: tuple[str, ...]
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class RepositoryBatchResult:
    run_id: str
    status: Literal["complete", "partial", "failed"]
    requested_count: int
    raw_stored_count: int
    processed_stored_count: int
    excluded_count: int
    duplicate_count: int
    failed_count: int
    items: tuple[RepositoryBatchItem, ...]


def parse_repository_target(target: str) -> str:
    """Return the supplied owner/repo casing after strict slug or HTTPS URL validation."""
    if not isinstance(target, str):
        raise ValueError("Repository target must be a string")
    slug = target.strip()
    if slug.lower().startswith("https://"):
        validate_http_url(slug)
        parts = urlsplit(slug)
        if (
            parts.hostname != "github.com"
            or parts.username is not None
            or parts.password is not None
            or parts.port is not None
            or "?" in slug
            or "#" in slug
        ):
            raise ValueError(
                "Expected an HTTPS GitHub repository URL without credentials or options"
            )
        slug = parts.path.removeprefix("/").removesuffix("/")
    canonical_github_url(slug)
    return slug


def _repository_public_scope(
    payload: dict,
) -> tuple[Literal["public", "excluded", "unverified"], tuple[str, ...]]:
    private = payload.get("private")
    visibility = payload.get("visibility")
    if private is not None and type(private) is not bool:
        raise ValueError("Invalid repository visibility metadata")
    if visibility is not None and (
        not isinstance(visibility, str) or visibility not in {"public", "private", "internal"}
    ):
        raise ValueError("Invalid repository visibility metadata")
    if private is True or visibility == "private":
        return "excluded", ("private_repository",)
    if visibility == "internal":
        return "excluded", ("internal_repository",)
    if private is False or visibility == "public":
        return "public", ()
    return "unverified", ()


def _repository_exclusion_reasons(repository: GitHubRepository) -> tuple[str, ...]:
    return tuple(
        reason
        for excluded, reason in (
            (repository.visibility == "private", "private_repository"),
            (repository.visibility == "internal", "internal_repository"),
            (repository.is_fork, "fork_repository"),
            (repository.is_template, "template_repository"),
            (repository.mirror_url is not None, "mirror_repository"),
        )
        if excluded
    )


def _batch_failure(
    target: str, code: str, message: str, repo_id: str | None = None
) -> RepositoryBatchItem:
    return RepositoryBatchItem(target, "failed", repo_id, (), code, message)


def _repository_row(record: GitHubRepository) -> dict:
    return {
        "schema_version": record.schema_version,
        "repo_id": record.identity.repo_id,
        "github_repo_id": record.identity.github_repo_id,
        "repo_full_name": record.identity.repo_full_name,
        "repo_url": record.identity.repo_url,
        "retrieved_at": record.provenance.retrieved_at,
        "valid_at": record.provenance.valid_at,
        "available_at": record.provenance.available_at,
        "temporal_basis": record.provenance.temporal_basis,
        "source": record.provenance.source,
        "source_url": record.provenance.source_url,
        "raw_record_id": record.provenance.raw_record_ids[0],
        "is_archived": record.is_archived,
        "is_fork": record.is_fork,
        "is_template": record.is_template,
        "stargazers_count": record.stargazers_count,
        "forks_count": record.forks_count,
        "open_issues_and_prs_count": record.open_issues_and_prs_count,
    }


class RepositoryService:
    def __init__(self, client=None, *, storage_paths: StoragePaths | None = None):
        self.collector = GitHubRepositoryContractCollector(client)
        self.normalizer = GitHubRepositoryContractNormalizer()
        self.paths = storage_paths if storage_paths is not None else paths
        self.raw_storage = JsonlStorage()
        self.parquet_storage = ParquetStorage()

    def close(self) -> None:
        self.collector.close()

    def _collect_repository(self, slug: str, run_id: str) -> CollectionResult:
        request = CollectionRequest(
            schema_version=SCHEMA_VERSION,
            record_type="collection_request",
            request_id=str(uuid4()),
            run_id=run_id,
            collector_name="github_repository_contract",
            source="github_rest",
            subjects=[
                CollectionSubject(
                    repo_id=None,
                    repo_full_name=slug,
                    package_id=None,
                    url=None,
                )
            ],
            record_kinds=["github_repository"],
            time_range=None,
            cutoff_at=None,
            cursor=None,
            max_pages=1,
            max_records=1,
        )
        collected = self.collector.collect(request)
        validate_collection_pair(request, collected)
        return collected

    def _normalize_repository(self, raw: RawRecord) -> GitHubRepository:
        request = NormalizationRequest(
            schema_version=SCHEMA_VERSION,
            record_type="normalization_request",
            request_id=str(uuid4()),
            run_id=raw.run_id,
            records=[raw],
        )
        normalized = self.normalizer.normalize(request)
        validate_normalization_pair(request, normalized)
        if (
            normalized.status != "complete"
            or len(normalized.records) != 1
            or not isinstance(normalized.records[0], GitHubRepository)
        ):
            raise CollectionError("Expected one valid normalized repository")
        return normalized.records[0]

    def _write_processed_rows(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        schema = {key: pl.String for key in rows[0]}
        schema.update({key: pl.Boolean for key in ("is_archived", "is_fork", "is_template")})
        schema.update(
            {
                key: pl.Int64
                for key in ("stargazers_count", "forks_count", "open_issues_and_prs_count")
            }
        )
        try:
            frame = pl.DataFrame(rows, schema=schema)
            processed_file = self.paths.processed_path("repositories")
            existing = self.parquet_storage.read(processed_file)
            # An existing empty table also needs the canonical schema.
            if processed_file.exists() or existing.width:
                if existing.schema != frame.schema:
                    raise CollectionError(
                        "Existing processed repository schema requires explicit migration"
                    )
                frame = pl.concat([existing, frame])
            self.parquet_storage.write(processed_file, frame)
        except (OSError, StorageError, pl.exceptions.PolarsError) as error:
            raise CollectionError("Unable to persist processed repository data") from error
        return len(rows)

    def collect_and_store(self, owner: str, repo: str) -> GitHubRepository:
        collected = self._collect_repository(f"{owner}/{repo}", str(uuid4()))
        if collected.status != "complete":
            raise CollectionError(collected.errors[0].message)
        if len(collected.records) != 1:
            raise CollectionError("Expected one raw repository")
        raw = collected.records[0]
        try:
            scope, _ = _repository_public_scope(raw.payload)
        except ValueError as error:
            raise CollectionError("Invalid repository visibility metadata") from error
        if scope == "excluded":
            raise CollectionError("Repository is outside the public metadata scope")
        if scope == "unverified":
            raise CollectionError("Repository public visibility could not be verified")
        self.raw_storage.append_contract_record(self.paths.raw_path("github", "repositories"), raw)
        record = self._normalize_repository(raw)
        reasons = _repository_exclusion_reasons(record)
        if reasons:
            raise CollectionError(
                "Repository excluded from processed output: " + ", ".join(reasons)
            )
        self._write_processed_rows([_repository_row(record)])
        return record

    def collect_many(self, targets: Sequence[str]) -> RepositoryBatchResult:
        """Collect current observations sequentially, isolating expected per-target failures."""
        if isinstance(targets, str | bytes) or not 1 <= len(targets) <= 1000:
            raise ValueError("A repository batch requires 1 to 1,000 targets")
        run_id = str(uuid4())
        items = []
        rows = []
        seen_slugs: dict[str, str | None] = {}
        seen_ids: set[str] = set()
        raw_stored_count = 0
        for target in targets:
            try:
                slug = parse_repository_target(target)
            except ValueError:
                # Never echo malformed input: it may be a URL containing credentials.
                items.append(
                    _batch_failure(
                        "<invalid target>",
                        "invalid_target",
                        "Expected owner/repo or HTTPS GitHub URL",
                    )
                )
                continue
            key = slug.lower()
            if key in seen_slugs:
                items.append(
                    RepositoryBatchItem(slug, "duplicate", seen_slugs[key], (), None, None)
                )
                continue
            seen_slugs[key] = None
            try:
                collected = self._collect_repository(slug, run_id)
            except ValueError:
                items.append(_batch_failure(slug, "validation_error", "Invalid collection result"))
                continue
            except CollectionError:
                items.append(
                    _batch_failure(slug, "collection_error", "Repository collection failed")
                )
                continue
            if collected.status != "complete":
                error = collected.errors[0]
                items.append(_batch_failure(slug, error.code, error.message))
                continue
            if len(collected.records) != 1:
                items.append(
                    _batch_failure(slug, "validation_error", "Expected one raw repository")
                )
                continue
            raw = collected.records[0]
            repo_id = raw.repo_id
            seen_slugs[key] = repo_id
            if repo_id in seen_ids:
                items.append(RepositoryBatchItem(slug, "duplicate", repo_id, (), None, None))
                continue
            seen_ids.add(repo_id)
            try:
                scope, reasons = _repository_public_scope(raw.payload)
            except ValueError:
                items.append(
                    _batch_failure(
                        slug, "validation_error", "Invalid repository visibility metadata", repo_id
                    )
                )
                continue
            if scope == "excluded":
                items.append(RepositoryBatchItem(slug, "excluded", repo_id, reasons, None, None))
                continue
            if scope == "unverified":
                items.append(
                    _batch_failure(
                        slug,
                        "visibility_unverified",
                        "Repository public visibility could not be verified",
                        repo_id,
                    )
                )
                continue
            try:
                self.raw_storage.append_contract_record(
                    self.paths.raw_path("github", "repositories"), raw
                )
            except (OSError, StorageError):
                items.append(
                    _batch_failure(slug, "storage_error", "Raw persistence failed", repo_id)
                )
                continue
            raw_stored_count += 1
            try:
                record = self._normalize_repository(raw)
            except (ValueError, CollectionError):
                items.append(
                    _batch_failure(
                        slug, "normalization_error", "Repository normalization failed", repo_id
                    )
                )
                continue
            reasons = _repository_exclusion_reasons(record)
            if reasons:
                items.append(RepositoryBatchItem(slug, "excluded", repo_id, reasons, None, None))
                continue
            rows.append(_repository_row(record))
            items.append(RepositoryBatchItem(slug, "stored", repo_id, (), None, None))
        # A failed final write raises; no report can claim stored processed rows before it succeeds.
        processed_stored_count = self._write_processed_rows(rows)
        excluded_count = sum(item.status == "excluded" for item in items)
        duplicate_count = sum(item.status == "duplicate" for item in items)
        failed_count = sum(item.status == "failed" for item in items)
        status = (
            "complete"
            if not failed_count
            else "partial"
            if processed_stored_count or excluded_count
            else "failed"
        )
        return RepositoryBatchResult(
            run_id=run_id,
            status=status,
            requested_count=len(targets),
            raw_stored_count=raw_stored_count,
            processed_stored_count=processed_stored_count,
            excluded_count=excluded_count,
            duplicate_count=duplicate_count,
            failed_count=failed_count,
            items=tuple(items),
        )
