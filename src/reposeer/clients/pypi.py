"""PyPI JSON API client."""

from typing import Any

from reposeer.collection.http.client import HTTPClient


class PyPIClient:
    """Client for PyPI JSON metadata."""

    def __init__(self, base_url: str = "https://pypi.org"):
        self.http = HTTPClient(base_url=base_url)

    def get_package_info(self, package_name: str) -> dict[str, Any]:
        """Fetch metadata for a package from PyPI."""
        return self.http.get_json(f"/pypi/{package_name}/json")

    def close(self) -> None:
        self.http.close()
