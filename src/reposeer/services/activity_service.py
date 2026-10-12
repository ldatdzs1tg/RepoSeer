"""T-014 raw activity persistence for one verified repository and group."""

from collections.abc import Callable
from datetime import datetime
from uuid import uuid4

from reposeer.clients.github import GitHubClient
from reposeer.collectors.github.activity import ActivityGroup, GitHubActivityContractCollector
from reposeer.contracts import (
    SCHEMA_VERSION,
    CollectionRequest,
    CollectionResult,
    CollectionSubject,
    GitHubRepository,
    validate_collection_pair,
    validate_record,
)
from reposeer.exceptions import CollectionError, StorageError
from reposeer.storage.paths import StoragePaths, paths
from reposeer.storage.raw.jsonl import JsonlStorage


class ActivityService:
    """Append valid native raw observations; persistence errors never return success."""

    def __init__(
        self,
        client: GitHubClient | None = None,
        *,
        storage_paths: StoragePaths | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.client = client if client is not None else GitHubClient()
        self._owns_client = client is None
        self.paths = storage_paths if storage_paths is not None else paths
        self.clock = clock
        self.raw_storage = JsonlStorage()

    def collect_and_store(
        self,
        repository: GitHubRepository,
        group: ActivityGroup,
        *,
        max_pages: int = 10,
        max_records: int = 1000,
        cursor: str | None = None,
        run_id: str | None = None,
    ) -> CollectionResult:
        repository = validate_record(repository)
        if not isinstance(repository, GitHubRepository):
            raise TypeError("Expected canonical GitHubRepository")
        request = CollectionRequest(
            schema_version=SCHEMA_VERSION,
            record_type="collection_request",
            request_id=str(uuid4()),
            run_id=str(uuid4()) if run_id is None else run_id,
            collector_name=f"github_activity_{group}",
            source="github_rest",
            subjects=[
                CollectionSubject(
                    repo_id=repository.identity.repo_id,
                    repo_full_name=repository.identity.repo_full_name,
                    package_id=None,
                    url=None,
                )
            ],
            record_kinds=["repository_activity"],
            time_range=None,
            cutoff_at=None,
            cursor=cursor,
            max_pages=max_pages,
            max_records=max_records,
        )
        collector = GitHubActivityContractCollector(group, repository, self.client, clock=self.clock)
        result = collector.collect(request)
        validate_collection_pair(request, result)
        if result.records:
            try:
                destination = self.paths.raw_path("github", group)
                for record in result.records:
                    self.raw_storage.append_contract_record(destination, record)
            except (OSError, StorageError) as error:
                raise CollectionError("Unable to persist GitHub activity raw records") from error
        return result

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
            self._owns_client = False
