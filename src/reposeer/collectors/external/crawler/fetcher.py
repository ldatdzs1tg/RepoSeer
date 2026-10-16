"""HTML fetcher using HTTPX."""

from typing import Any, Self
from urllib.parse import urljoin

import httpx

from reposeer.collection.errors import handle_collection_error
from reposeer.collection.http.client import HTTPClient
from reposeer.collection.progress import CollectionProgress
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.exceptions import HTTPClientError


class HTMLFetcher:
    """Fetches raw web page HTML."""

    def __init__(
        self, http_client: HTTPClient | None = None, *, progress: CollectionProgress | None = None
    ):
        self._owns_http = http_client is None
        self.http = http_client if http_client is not None else HTTPClient()
        self.progress = progress

    def fetch_response(
        self,
        url: str,
        *,
        policy: URLPolicy,
        max_redirects: int = 5,
        max_response_bytes: int = 5_000_000,
        accept: str = "text/html,application/xhtml+xml",
    ) -> httpx.Response:
        """Bounded public GET; inspect each redirect before following it.

        Errors propagate to the crawler for per-candidate accounting. Bypass the
        TTL cache so fetched_at always describes the actual observation.
        """
        seen = set()
        for _ in range(max_redirects + 1):
            policy.validate(url)
            if url in seen:
                raise ValueError("redirect_loop")
            seen.add(url)
            response = self.http.get(
                url,
                headers={"Accept": accept},
                use_cache=False,
                follow_redirects=False,
                max_response_bytes=max_response_bytes,
                public_request=True,
            )
            if not response.is_redirect:
                return response
            location = response.headers.get("location")
            if not location:
                raise ValueError("redirect_missing_location")
            url = urljoin(str(response.url), location)
        raise ValueError("redirect_limit")

    def fetch(self, url: str) -> str | None:
        """Fetch HTML text from URL."""
        try:
            resp = self.http.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
        except HTTPClientError as error:
            # The HTTP error message already contains an origin/path without query secrets.
            handle_collection_error(error, "external.html", progress=self.progress)
            return None
        if self.progress is not None:
            self.progress.record_success("external.html.url", url)
        return resp.text

    def close(self) -> None:
        """Close owned sessions; leave injected shared sessions to their caller."""
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
