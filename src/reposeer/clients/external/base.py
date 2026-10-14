"""Base discovery provider interface according to T-016."""

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

from reposeer.schemas.external.candidate import ExternalCandidate


class DiscoveryPageResult(BaseModel):
    """Paginated result for external discovery providers."""

    candidates: list[ExternalCandidate] = Field(default_factory=list)
    page_number: int = Field(default=1, ge=1)
    has_more: bool = False
    next_cursor: str | None = None
    total_hits_reported: int | None = None
    observed_from: datetime | None = None
    observed_to: datetime | None = None


class DiscoveryProvider(ABC):
    """Abstract provider for external signals and news discovery."""

    provider_name: str = "base"
    supports_historical_window: bool = False

    @abstractmethod
    def search_page(
        self,
        query: str,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> DiscoveryPageResult:
        """Search a single paginated page of external candidates."""
        pass

    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        """Search upstream provider for articles or mentions."""
        result = self.search_page(query, page_size=limit)
        return result.candidates[:limit]


