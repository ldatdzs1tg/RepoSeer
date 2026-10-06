"""GitHub pull requests collector."""

from datetime import datetime

from reposeer.clients.github import GitHubClient
from reposeer.schemas.github.pull_request import PullRequestRecord


class PullRequestCollector:
    def __init__(self, client: GitHubClient | None = None):
        self.client = client or GitHubClient()

    def collect(self, owner: str, repo: str, max_pages: int = 3) -> list[PullRequestRecord]:
        full_name = f"{owner}/{repo}"
        records: list[PullRequestRecord] = []
        for page in range(1, max_pages + 1):
            items = self.client.list_pull_requests(owner, repo, per_page=100, page=page)
            if not items:
                break
            for item in items:
                records.append(
                    PullRequestRecord(
                        number=item.get("number", 0),
                        repo_full_name=full_name,
                        title=item.get("title", ""),
                        state=item.get("state", "open"),
                        created_at=datetime.fromisoformat(item["created_at"]),
                        closed_at=datetime.fromisoformat(item["closed_at"])
                        if item.get("closed_at")
                        else None,
                        merged_at=datetime.fromisoformat(item["merged_at"])
                        if item.get("merged_at")
                        else None,
                        is_merged=item.get("merged_at") is not None,
                        comments_count=item.get("comments", 0),
                    )
                )
        return records
