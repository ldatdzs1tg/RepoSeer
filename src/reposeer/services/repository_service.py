"""Canonical repository collection, raw persistence and processed projection."""

from uuid import uuid4

import polars as pl

from reposeer.contracts import (
    SCHEMA_VERSION,
    CollectionRequest,
    CollectionSubject,
    GitHubRepository,
    NormalizationRequest,
    validate_collection_pair,
    validate_normalization_pair,
)
from reposeer.contracts.adapters import (
    GitHubRepositoryContractCollector,
    GitHubRepositoryContractNormalizer,
)
from reposeer.exceptions import CollectionError
from reposeer.storage.paths import StoragePaths, paths
from reposeer.storage.processed.parquet import ParquetStorage
from reposeer.storage.raw.jsonl import JsonlStorage


class RepositoryService:
    def __init__(self, client=None, *, storage_paths: StoragePaths | None = None):
        self.collector = GitHubRepositoryContractCollector(client)
        self.normalizer = GitHubRepositoryContractNormalizer()
        self.paths = storage_paths if storage_paths is not None else paths
        self.raw_storage = JsonlStorage()
        self.parquet_storage = ParquetStorage()

    def close(self) -> None:
        self.collector.close()

    def collect_and_store(self, owner: str, repo: str) -> GitHubRepository:
        request = CollectionRequest(
            schema_version=SCHEMA_VERSION,
            record_type="collection_request",
            request_id=str(uuid4()),
            run_id=str(uuid4()),
            collector_name="github_repository_contract",
            source="github_rest",
            subjects=[
                CollectionSubject(
                    repo_id=None,
                    repo_full_name=f"{owner}/{repo}",
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
        if collected.status != "complete":
            raise CollectionError(collected.errors[0].message)
        raw_file = self.paths.raw_path("github", "repositories")
        for raw in collected.records:
            self.raw_storage.append_contract_record(raw_file, raw)
        normalization = NormalizationRequest(
            schema_version=SCHEMA_VERSION,
            record_type="normalization_request",
            request_id=str(uuid4()),
            run_id=request.run_id,
            records=collected.records,
        )
        normalized = self.normalizer.normalize(normalization)
        validate_normalization_pair(normalization, normalized)
        if normalized.status != "complete" or len(normalized.records) != 1:
            raise CollectionError(
                normalized.errors[0].message
                if normalized.errors
                else "Expected one normalized repository"
            )
        record = normalized.records[0]
        row = {
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
        schema = {key: pl.String for key in row}
        schema.update({key: pl.Boolean for key in ("is_archived", "is_fork", "is_template")})
        schema.update(
            {
                key: pl.Int64
                for key in ("stargazers_count", "forks_count", "open_issues_and_prs_count")
            }
        )
        frame = pl.DataFrame([row], schema=schema)
        processed_file = self.paths.processed_path("repositories")
        existing = self.parquet_storage.read(processed_file)
        if not existing.is_empty():
            # Reject legacy/incompatible tables rather than silently coercing or rewriting user data.
            if existing.schema != frame.schema:
                raise CollectionError(
                    "Existing processed repository schema requires explicit migration"
                )
            frame = pl.concat([existing, frame])
        self.parquet_storage.write(processed_file, frame)
        return record
