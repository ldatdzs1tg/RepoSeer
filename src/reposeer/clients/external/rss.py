"""RSS feed discovery provider stub."""

from reposeer.clients.external.base import DiscoveryProvider
from reposeer.schemas.external.candidate import ExternalCandidate


class RSSProvider(DiscoveryProvider):
    """Generic RSS feed discovery provider."""

    def __init__(self, feed_urls: list[str] | None = None):
        self.feed_urls = feed_urls or []

    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        """Search entries from configured RSS feeds."""
        # Extensible stub for RSS parsing
        return []
