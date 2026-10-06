"""Hacker News Algolia search client."""

import logging

from reposeer.clients.external.base import DiscoveryProvider
from reposeer.collection.http.client import HTTPClient
from reposeer.schemas.external.candidate import ExternalCandidate

logger = logging.getLogger("reposeer.clients.external.hackernews")


class HackerNewsClient(DiscoveryProvider):
    """Discovery provider querying Hacker News Algolia search API."""

    def __init__(self, base_url: str = "https://hn.algolia.com/api/v1"):
        self.http = HTTPClient(base_url=base_url)

    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        """Search HN stories matching query."""
        try:
            data = self.http.get_json(
                "/search", params={"query": query, "hitsPerPage": limit, "tags": "story"}
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
            return candidates
        except Exception as e:
            logger.warning("Failed to search Hacker News for '%s': %s", query, e)
            return []
