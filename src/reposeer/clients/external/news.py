"""News API discovery provider stub."""

from reposeer.clients.external.base import DiscoveryProvider
from reposeer.schemas.external.candidate import ExternalCandidate


class NewsAPIProvider(DiscoveryProvider):
    """NewsAPI provider stub."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key

    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        return []
