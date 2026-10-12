"""GitHub repository collector."""

from typing import Any

from reposeer.clients.github import GitHubClient
from reposeer.collection.errors import handle_collection_error
from reposeer.collection.progress import CollectionProgress
from reposeer.exceptions import HTTPClientError
from reposeer.schemas.github.repository import RepositoryRecord


class RepositoryCollector:
    """Collects and maps repository metadata into RepositoryRecord."""

    def __init__(
        self, client: GitHubClient | None = None, *, progress: CollectionProgress | None = None
    ):
        self.client = client or GitHubClient()
        self.progress = progress

    def collect(self, owner: str, repo: str) -> RepositoryRecord:
        """Fetch and parse repository."""
        try:
            data = self.client.get_repository(owner, repo)
            record = self._parse_repository(data, owner, repo)
        except (HTTPClientError, ValueError, TypeError, AttributeError) as error:
            handle_collection_error(error, f"{owner}/{repo}", progress=self.progress)
            raise
        if self.progress is not None:
            self.progress.record_success("github.repository", record.full_name)
        return record

    @staticmethod
    def _parse_repository(data: dict[str, Any], owner: str, repo: str) -> RepositoryRecord:
        license_info = data.get("license") or {}
        return RepositoryRecord(
            id=data.get("id"),
            owner=owner,
            name=repo,
            full_name=data.get("full_name", f"{owner}/{repo}"),
            description=data.get("description"),
            html_url=data.get("html_url"),
            stargazers_count=data.get("stargazers_count", 0),
            forks_count=data.get("forks_count", 0),
            watchers_count=data.get("watchers_count", 0),
            open_issues_count=data.get("open_issues_count", 0),
            language=data.get("language"),
            license_key=license_info.get("key"),
            license_name=license_info.get("name"),
            topics=data.get("topics", []),
            default_branch=data.get("default_branch", "main"),
            archived=data.get("archived", False),
            fork=data.get("fork", False),
            size=data.get("size", 0),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )
