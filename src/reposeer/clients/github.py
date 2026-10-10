"""GitHub REST API Client implementing T-013 and T-014."""

import logging
from typing import Any, Self

from reposeer.collection.http.client import HTTPClient
from reposeer.config import settings
from reposeer.exceptions import HTTPClientError

logger = logging.getLogger("reposeer.clients.github")


class GitHubClient:
    """REST Client for the GitHub v3 API."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str = "https://api.github.com",
        *,
        http_client: HTTPClient | None = None,
    ):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        auth_token = token or settings.github_token
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        self.base_url = base_url.rstrip("/")
        self._headers = headers
        self._owns_http = http_client is None
        self.http = http_client if http_client is not None else HTTPClient(base_url=base_url)

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.http.get_json(f"{self.base_url}{path}", params=params, headers=self._headers)

    def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        """Fetch metadata for a single repository."""
        logger.info("Fetching repository metadata for %s/%s", owner, repo)
        return self._get_json(f"/repos/{owner}/{repo}")

    def list_commits(
        self, owner: str, repo: str, per_page: int = 100, page: int = 1
    ) -> list[dict[str, Any]]:
        """Fetch repository commits."""
        return self._get_json(
            f"/repos/{owner}/{repo}/commits", params={"per_page": per_page, "page": page}
        )

    def list_issues(
        self, owner: str, repo: str, state: str = "all", per_page: int = 100, page: int = 1
    ) -> list[dict[str, Any]]:
        """Fetch repository issues."""
        return self._get_json(
            f"/repos/{owner}/{repo}/issues",
            params={"state": state, "per_page": per_page, "page": page},
        )

    def list_pull_requests(
        self, owner: str, repo: str, state: str = "all", per_page: int = 100, page: int = 1
    ) -> list[dict[str, Any]]:
        """Fetch repository pull requests."""
        return self._get_json(
            f"/repos/{owner}/{repo}/pulls",
            params={"state": state, "per_page": per_page, "page": page},
        )

    def list_releases(
        self, owner: str, repo: str, per_page: int = 100, page: int = 1
    ) -> list[dict[str, Any]]:
        """Fetch repository releases."""
        return self._get_json(
            f"/repos/{owner}/{repo}/releases", params={"per_page": per_page, "page": page}
        )

    def list_contributors(
        self, owner: str, repo: str, per_page: int = 100, page: int = 1
    ) -> list[dict[str, Any]]:
        """Fetch repository contributors."""
        return self._get_json(
            f"/repos/{owner}/{repo}/contributors", params={"per_page": per_page, "page": page}
        )

    def get_file_content(self, owner: str, repo: str, path: str, ref: str = "HEAD") -> str | None:
        """Fetch raw file content from repository if exists."""
        try:
            resp = self.http.get(
                f"{self.base_url}/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
                headers={**self._headers, "Accept": "application/vnd.github.raw+json"},
            )
            return resp.text
        except HTTPClientError as error:
            if error.status_code == 404:
                return None
            raise

    def close(self) -> None:
        """Close connection."""
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
