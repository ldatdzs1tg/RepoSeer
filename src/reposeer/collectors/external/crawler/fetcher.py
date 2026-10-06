"""HTML fetcher using HTTPX."""

import logging

from reposeer.collection.http.client import HTTPClient
from reposeer.exceptions import HTTPClientError

logger = logging.getLogger("reposeer.crawler.fetcher")


class HTMLFetcher:
    """Fetches raw web page HTML."""

    def __init__(self, http_client: HTTPClient | None = None):
        self.http = http_client or HTTPClient()

    def fetch(self, url: str) -> str | None:
        """Fetch HTML text from URL."""
        try:
            resp = self.http.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
            return resp.text
        except HTTPClientError as e:
            logger.warning("Failed to fetch HTML for '%s': %s", url, e)
            return None
