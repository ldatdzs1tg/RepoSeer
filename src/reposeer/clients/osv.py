"""OSV vulnerability API client."""

from typing import Any

from reposeer.collection.http.client import HTTPClient


class OSVClient:
    """Client for Open Source Vulnerabilities (OSV) API."""

    def __init__(self, base_url: str = "https://api.osv.dev/v1"):
        self.http = HTTPClient(base_url=base_url)

    def query_vulnerabilities(
        self, package_name: str, ecosystem: str = "PyPI"
    ) -> list[dict[str, Any]]:
        """Query vulnerabilities for a package."""
        try:
            res = self.http.get_json(
                f"/query?package.name={package_name}&package.ecosystem={ecosystem}"
            )
            return res.get("vulns", [])
        except Exception:
            return []

    def close(self) -> None:
        self.http.close()
