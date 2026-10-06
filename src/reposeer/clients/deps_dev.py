"""Deps.dev API client."""

from typing import Any

from reposeer.collection.http.client import HTTPClient


class DepsDevClient:
    """Client for Google deps.dev API."""

    def __init__(self, base_url: str = "https://api.deps.dev/v3"):
        self.http = HTTPClient(base_url=base_url)

    def get_package(self, system: str, name: str) -> dict[str, Any]:
        """Fetch package information from deps.dev."""
        return self.http.get_json(f"/systems/{system}/packages/{name}")

    def close(self) -> None:
        self.http.close()
