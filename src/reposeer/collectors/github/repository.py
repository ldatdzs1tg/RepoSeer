"""GitHub repository collector."""

import logging

from reposeer.clients.github import GitHubClient
from reposeer.schemas.github.repository import RepositoryRecord

logger = logging.getLogger("reposeer.collectors.github.repo")


class RepositoryCollector:
    """Collects and maps repository metadata into RepositoryRecord."""

    def __init__(self, client: GitHubClient | None = None):
        self.client = client or GitHubClient()

    def collect(self, owner: str, repo: str) -> RepositoryRecord:
        """Fetch and parse repository."""
        data = self.client.get_repository(owner, repo)
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
