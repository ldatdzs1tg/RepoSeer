"""GitHub issues collector."""

from datetime import datetime

from reposeer.clients.github import GitHubClient
from reposeer.schemas.github.issue import IssueRecord


class IssueCollector:
    def __init__(self, client: GitHubClient | None = None):
        self.client = client or GitHubClient()

    def collect(self, owner: str, repo: str, max_pages: int = 3) -> list[IssueRecord]:
        full_name = f"{owner}/{repo}"
        records: list[IssueRecord] = []
        for page in range(1, max_pages + 1):
            items = self.client.list_issues(owner, repo, per_page=100, page=page)
            if not items:
                break
            for item in items:
                records.append(
                    IssueRecord(
                        number=item.get("number", 0),
                        repo_full_name=full_name,
                        title=item.get("title", ""),
                        state=item.get("state", "open"),
                        created_at=datetime.fromisoformat(item["created_at"]),
                        closed_at=datetime.fromisoformat(item["closed_at"])
                        if item.get("closed_at")
                        else None,
                        comments_count=item.get("comments", 0),
                        is_pull_request="pull_request" in item,
                    )
                )
        return records
