"""Project README collector."""

from reposeer.clients.github import GitHubClient
from reposeer.schemas.project.document import ProjectDocument


class ReadmeCollector:
    def __init__(self, client: GitHubClient | None = None):
        self.client = client or GitHubClient()

    def collect(self, owner: str, repo: str) -> ProjectDocument | None:
        content = self.client.get_file_content(owner, repo, "README.md")
        if content is None:
            content = self.client.get_file_content(owner, repo, "readme.md")
        if content is not None:
            return ProjectDocument(
                repo_full_name=f"{owner}/{repo}",
                doc_type="readme",
                content=content,
                format="markdown",
            )
        return None
