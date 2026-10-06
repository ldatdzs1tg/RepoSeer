"""GitHub commits collector with checkpointing."""

from datetime import datetime

from reposeer.clients.github import GitHubClient
from reposeer.schemas.github.commit import CommitRecord


class CommitCollector:
    def __init__(self, client: GitHubClient | None = None):
        self.client = client or GitHubClient()

    def collect(self, owner: str, repo: str, max_pages: int = 5) -> list[CommitRecord]:
        full_name = f"{owner}/{repo}"
        records: list[CommitRecord] = []
        for page in range(1, max_pages + 1):
            commits = self.client.list_commits(owner, repo, per_page=100, page=page)
            if not commits:
                break
            for item in commits:
                commit_info = item.get("commit", {})
                author_info = commit_info.get("author", {})
                date_str = author_info.get("date")
                records.append(
                    CommitRecord(
                        sha=item.get("sha", ""),
                        repo_full_name=full_name,
                        author_name=author_info.get("name"),
                        author_email=author_info.get("email"),
                        committed_at=datetime.fromisoformat(date_str)
                        if date_str
                        else datetime.utcnow(),
                        message=commit_info.get("message", ""),
                    )
                )
        return records
