"""HTML fetcher using HTTPX."""

from typing import Any, Self

from reposeer.collection.errors import handle_collection_error
from reposeer.collection.http.client import HTTPClient
from reposeer.collection.progress import CollectionProgress
from reposeer.exceptions import HTTPClientError


class HTMLFetcher:
    """Fetches raw web page HTML."""

    def __init__(
        self, http_client: HTTPClient | None = None, *, progress: CollectionProgress | None = None
    ):
        self._owns_http = http_client is None
        self.http = http_client if http_client is not None else HTTPClient()
        self.progress = progress

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
