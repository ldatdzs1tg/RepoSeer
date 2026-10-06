"""GitHub releases collector."""

from datetime import datetime

from reposeer.clients.github import GitHubClient
from reposeer.schemas.github.release import ReleaseRecord


class ReleaseCollector:
    def __init__(self, client: GitHubClient | None = None):
        self.client = client or GitHubClient()

    def collect(self, owner: str, repo: str, max_pages: int = 2) -> list[ReleaseRecord]:
        full_name = f"{owner}/{repo}"
        records: list[ReleaseRecord] = []
        for page in range(1, max_pages + 1):
            items = self.client.list_releases(owner, repo, per_page=100, page=page)
            if not items:
                break
            for item in items:
                pub_date = item.get("published_at")
                records.append(
                    ReleaseRecord(
                        tag_name=item.get("tag_name", ""),
                        name=item.get("name"),
                        repo_full_name=full_name,
                        published_at=datetime.fromisoformat(pub_date) if pub_date else None,
                        is_prerelease=item.get("prerelease", False),
                        is_draft=item.get("draft", False),
                    )
                )
        return records
