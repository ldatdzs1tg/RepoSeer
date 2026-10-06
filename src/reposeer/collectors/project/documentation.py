"""Project documentation files collector."""

from reposeer.clients.github import GitHubClient
from reposeer.schemas.project.document import ProjectDocument


class DocumentationCollector:
    def __init__(self, client: GitHubClient | None = None):
        self.client = client or GitHubClient()

    def collect(self, owner: str, repo: str) -> list[ProjectDocument]:
        return []
