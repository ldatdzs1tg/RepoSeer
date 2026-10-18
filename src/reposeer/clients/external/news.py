"""News API discovery provider stub."""

from datetime import datetime

from reposeer.clients.external.base import DiscoveryPageResult, DiscoveryProvider
from reposeer.schemas.external.candidate import ExternalCandidate


class NewsAPIProvider(DiscoveryProvider):
    """NewsAPI provider stub."""

    provider_name: str = "news"
    supports_historical_window: bool = True
    is_available: bool = False

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key

    def search_page(
        self,
        query: str,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> DiscoveryPageResult:
        """Search entries from NewsAPI (paginated stub)."""
        return DiscoveryPageResult(candidates=[], page_number=1, has_more=False)

    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        return []
