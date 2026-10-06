"""Shared HTTP client layer wrapping HTTPX with retry and rate limiting."""

import logging
from typing import Any

import httpx

from reposeer.collection.http.rate_limit import RateLimitTracker
from reposeer.collection.http.retry import get_default_retry_decorator
from reposeer.config import settings
from reposeer.exceptions import HTTPClientError, RateLimitExceededError

logger = logging.getLogger("reposeer.http.client")


class HTTPClient:
    """Unified HTTP client for synchronous and asynchronous requests."""

    def __init__(
        self,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ):
        self.base_url = base_url
        default_headers = {
            "User-Agent": "RepoSeer/0.1.0",
            "Accept": "application/json",
        }
        if headers:
            default_headers.update(headers)

        self.timeout = timeout or settings.collection.timeout_seconds
        self.max_retries = max_retries or settings.collection.max_retries
        self.rate_limiter = RateLimitTracker()

        self._sync_client = httpx.Client(
            base_url=self.base_url,
            headers=default_headers,
            timeout=self.timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        """Close underlying HTTP sessions."""
        self._sync_client.close()

    def __enter__(self) -> "HTTPClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def get(
        self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        """Execute GET request with rate limiting and exponential backoff retry."""
        self.rate_limiter.check_and_wait_if_needed()

        retry_dec = get_default_retry_decorator(max_attempts=self.max_retries)

        @retry_dec
        def _request() -> httpx.Response:
            response = self._sync_client.get(url, params=params, headers=headers)
            self.rate_limiter.update_from_headers(response.headers)
            if response.status_code == 429:
                raise RateLimitExceededError("Rate limit exceeded from server")
            response.raise_for_status()
            return response

        try:
            return _request()
        except httpx.HTTPStatusError as e:
            raise HTTPClientError(
                f"HTTP {e.response.status_code} error fetching {url}",
                status_code=e.response.status_code,
                response_body=e.response.text,
            ) from e
        except Exception as e:
            if isinstance(e, HTTPClientError):
                raise
            raise HTTPClientError(f"Request failed for {url}: {e}") from e

    def get_json(
        self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
    ) -> Any:
        """Convenience method returning parsed JSON."""
        response = self.get(url, params=params, headers=headers)
        return response.json()
