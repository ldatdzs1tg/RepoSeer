"""GitHub contributors collector."""

from reposeer.clients.github import GitHubClient
from reposeer.schemas.github.contributor import ContributorRecord


class ContributorCollector:
    def __init__(self, client: GitHubClient | None = None):
        self.client = client or GitHubClient()

    def collect(self, owner: str, repo: str, max_pages: int = 2) -> list[ContributorRecord]:
        full_name = f"{owner}/{repo}"
        records: list[ContributorRecord] = []
        for page in range(1, max_pages + 1):
            items = self.client.list_contributors(owner, repo, per_page=100, page=page)
            if not items:
                break
            for item in items:
                records.append(
                    ContributorRecord(
                        login=item.get("login", ""),
                        repo_full_name=full_name,
                        contributions=item.get("contributions", 0),
                        contributor_type=item.get("type", "User"),
                        avatar_url=item.get("avatar_url"),
                    )
                )
        return records
