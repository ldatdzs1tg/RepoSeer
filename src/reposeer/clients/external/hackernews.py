"""Hacker News Algolia search client."""

from typing import Any, Self

from reposeer.clients.external.base import DiscoveryProvider
from reposeer.collection.errors import handle_collection_error
from reposeer.collection.http.client import HTTPClient
from reposeer.collection.progress import CollectionProgress
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
    ):
        self.base_url = base_url.rstrip("/")
        self._owns_http = http_client is None
        self.http = http_client if http_client is not None else HTTPClient(base_url=base_url)
        self.progress = progress

    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        """Search HN stories matching query."""
        try:
            data = self.http.get_json(
                f"{self.base_url}/search",
                params={"query": query, "hitsPerPage": limit, "tags": "story"},
                headers={"Accept": "application/json"},
            )
            candidates = []
            for hit in data.get("hits", []):
                url = (
                    hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                )
                title = hit.get("title") or "Untitled"
                candidates.append(
                    ExternalCandidate(
                        title=title,
                        url=url,
                        source="hackernews",
                        query=query,
                        score=float(hit.get("points") or 0.0),
                    )
                )
        except (HTTPClientError, ValueError, TypeError, AttributeError) as error:
            handle_collection_error(error, f"hackernews:{query}", progress=self.progress)
            return []
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
