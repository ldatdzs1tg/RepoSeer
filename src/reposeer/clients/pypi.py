"""PyPI JSON API client."""

from typing import Any

from reposeer.collection.http.client import HTTPClient


class PyPIClient:
    """Client for PyPI JSON metadata."""

    def __init__(self, base_url: str = "https://pypi.org", *, http_client: HTTPClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_http = http_client is None
        self.http = http_client if http_client is not None else HTTPClient(base_url=base_url)

    def get_package_info(self, package_name: str) -> dict[str, Any]:
        """Fetch metadata for a package from PyPI."""
        return self.http.get_json(f"{self.base_url}/pypi/{package_name}/json")

    def close(self) -> None:
        if self._owns_http:
            self.http.close()
            self._owns_http = False
