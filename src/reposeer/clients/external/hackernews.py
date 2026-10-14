"""Hacker News Algolia search client."""

from collections.abc import Callable
from datetime import UTC, datetime
from math import isfinite
from typing import Any, Self

from reposeer.clients.external.base import DiscoveryProvider
from reposeer.collection.errors import handle_collection_error
from reposeer.collection.http.client import HTTPClient
from reposeer.collection.progress import CollectionProgress
from reposeer.contracts.identity import validate_http_url
from reposeer.exceptions import HTTPClientError
from reposeer.schemas.external.candidate import ExternalCandidate


class HackerNewsClient(DiscoveryProvider):
    """Discovery provider querying Hacker News Algolia search API."""

    def __init__(
        self,
        base_url: str = "https://hn.algolia.com/api/v1",
        *,
        http_client: HTTPClient | None = None,
        progress: CollectionProgress | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._owns_http = http_client is None
        self.http = http_client if http_client is not None else HTTPClient(base_url=base_url)
        self.progress = progress
        self.clock = clock or (lambda: datetime.now(UTC))

    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        """Search HN stories matching query."""
        if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("query and bounded positive limit are required")
        try:
            data = self.http.get_json(
                f"{self.base_url}/search",
                params={"query": query, "hitsPerPage": limit, "tags": "story"},
                headers={"Accept": "application/json"},
            )
            if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
                raise ValueError("invalid_hackernews_search_payload")
            candidates = []
            for hit in data["hits"]:
                if not isinstance(hit, dict) or not isinstance(hit.get("title"), str) or not hit["title"].strip():
                    raise ValueError("invalid_hackernews_hit")
                url = hit.get("url")
                if url is None:
                    identifier = hit.get("objectID")
                    if not isinstance(identifier, str) or not identifier.isdecimal() or int(identifier) <= 0:
                        raise ValueError("invalid_hackernews_story_id")
                    url = f"https://news.ycombinator.com/item?id={identifier}"
                if not isinstance(url, str):
                    raise ValueError("invalid_hackernews_url")
                validate_http_url(url)
                score = hit.get("points")
                if score is not None and (type(score) not in {int, float} or not isfinite(score)):
                    raise ValueError("invalid_hackernews_score")
                candidates.append(ExternalCandidate(
                    title=hit["title"], url=url, source="hackernews", query=query,
                    score=float(score) if score is not None else None, discovered_at=self.clock(),
                ))
        except (HTTPClientError, ValueError) as error:
            handle_collection_error(error, f"hackernews:{query}", progress=self.progress)
            raise
        if self.progress is not None:
            self.progress.record_success("external.hackernews.query", query, count=len(candidates))
        return candidates

    def close(self) -> None:
        """Close the HTTP session if this provider created it."""
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
