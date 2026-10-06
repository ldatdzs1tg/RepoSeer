"""Project CHANGELOG collector."""

from reposeer.clients.github import GitHubClient
from reposeer.schemas.project.document import ProjectDocument


class ChangelogCollector:
    def __init__(self, client: GitHubClient | None = None):
        self.client = client or GitHubClient()

    def collect(self, owner: str, repo: str) -> ProjectDocument | None:
        for name in ("CHANGELOG.md", "CHANGES.md", "HISTORY.md"):
            content = self.client.get_file_content(owner, repo, name)
            if content:
                return ProjectDocument(
                    repo_full_name=f"{owner}/{repo}",
                    doc_type="changelog",
                    content=content,
                    format="markdown",
                )
        return None
