"""RSS feed discovery provider stub."""

from datetime import datetime

from reposeer.clients.external.base import DiscoveryPageResult, DiscoveryProvider
from reposeer.schemas.external.candidate import ExternalCandidate


class RSSProvider(DiscoveryProvider):
    """Generic RSS feed discovery provider."""

    provider_name: str = "rss"
    supports_historical_window: bool = False

    def __init__(self, feed_urls: list[str] | None = None):
        self.feed_urls = feed_urls or []

    def search_page(
        self,
        query: str,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> DiscoveryPageResult:
        """Search entries from configured RSS feeds (paginated)."""
        return DiscoveryPageResult(candidates=[], page_number=1, has_more=False)

    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        """Search entries from configured RSS feeds."""
        return []
