"""Base discovery provider interface according to T-016."""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field

from reposeer.schemas.external.candidate import ExternalCandidate


def is_historical_discovery(*, cutoff_at: datetime | None, window_end: datetime | None,
                            now: datetime, explicit: bool = False) -> bool:
    """A rolling lookback is current; allow five minutes of request/start clock skew.

    An explicitly historical request or an upper bound older than that is a backfill.
    This is retrieval policy, never a proof that content was available at that bound.
    """
    upper = min(value for value in (cutoff_at, window_end) if value is not None) if (
        cutoff_at is not None or window_end is not None) else None
    return explicit or (upper is not None and upper < now - timedelta(minutes=5))


class DiscoveryPageResult(BaseModel):
    """Paginated result for external discovery providers."""

    candidates: list[ExternalCandidate] = Field(default_factory=list)
    page_number: int = Field(default=1, ge=1)
    has_more: bool = False
    next_cursor: str | None = None
    total_hits_reported: int | None = None
    native_items_observed: int | None = Field(default=None, ge=0)
    observed_from: datetime | None = None
    observed_to: datetime | None = None


class DiscoveryProvider(ABC):
    """Abstract provider for external signals and news discovery."""

    provider_name: str = "base"
    supports_historical_window: bool = False
    is_available: bool = True
    initial_cursor: str = "1"

    def execution_policy(self) -> dict[str, Any]:
        """Public effective filters only. Never serialize API keys or authentication parameters."""
        parsed = urlsplit(getattr(self, "base_url", ""))
        host = parsed.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
        origin = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        return {"adapter": type(self).__name__, "base_url": origin,
                "site": getattr(self, "site", None),
                "filters": getattr(self, "search_filters", {}),
                "supports_historical_window": self.supports_historical_window, "is_available": self.is_available}

    def validate_cursor(self, cursor: str) -> None:
        if not cursor.isdecimal() or int(cursor) < int(self.initial_cursor):
            raise ValueError(f"Invalid cursor for {self.provider_name}")

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


