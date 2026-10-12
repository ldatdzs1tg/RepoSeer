"""Shared HTTP client layer wrapping HTTPX with retry and rate limiting."""

import logging
import math
import time
from collections.abc import Callable
from typing import Any

import httpx

from reposeer.collection.http.cache import ResponseCache
from reposeer.collection.http.rate_limit import RateLimitTracker, is_rate_limited_response
from reposeer.collection.http.retry import get_default_retry_decorator
from reposeer.config import settings
from reposeer.exceptions import HTTPClientError, RateLimitExceededError

logger = logging.getLogger("reposeer.http.client")


class HTTPClient:
    """Synchronous HTTP session shared by API collectors and HTML crawlers.

    ``max_retries`` retains the existing meaning of total attempts (default 3).
    Use 1 for a single attempt. Cache is optional and disabled by default.
    """

    def __init__(
        self,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        *,
        backoff_factor: float | None = None,
        max_backoff_seconds: float | None = None,
        cache: ResponseCache | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.base_url = base_url
        default_headers = httpx.Headers(
            {
                "User-Agent": "RepoSeer/0.1.0",
                "Accept": "application/json",
            }
        )
        if headers:
            default_headers.update(headers)

        config = settings.collection
        self.timeout = config.timeout_seconds if timeout is None else timeout
        self.max_retries = config.max_retries if max_retries is None else max_retries
        self.backoff_factor = config.backoff_factor if backoff_factor is None else backoff_factor
        self.max_backoff_seconds = (
            config.max_backoff_seconds if max_backoff_seconds is None else max_backoff_seconds
        )
        if self.timeout <= 0 or not math.isfinite(self.timeout):
            raise ValueError("timeout must be finite and positive")
        if not isinstance(self.max_retries, int) or self.max_retries < 1:
            raise ValueError("max_retries must be a positive total attempt count")
        if any(
            value < 0 or not math.isfinite(value)
            for value in (self.backoff_factor, self.max_backoff_seconds)
        ):
            raise ValueError("Backoff waits must be finite and non-negative")
        self._clock = clock or time.time
        self._sleep = sleep or time.sleep
        self.rate_limiter = self._new_rate_limiter()
        self._rate_limiters: dict[tuple[str, str, int | None], RateLimitTracker] = {}
        if base_url:
            self._rate_limiters[self._origin(httpx.URL(base_url))] = self.rate_limiter
        self.cache = cache
        if self.cache is None and config.cache_enabled:
            self.cache = ResponseCache(
                config.cache_dir or settings.storage.metadata_dir / "http_cache",
                ttl_seconds=config.cache_ttl_seconds,
                clock=self._clock,
            )

        self._sync_client = httpx.Client(
            base_url=self.base_url,
            headers=default_headers,
            timeout=self.timeout,
            follow_redirects=True,
            transport=transport,
        )

    @staticmethod
    def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
        return url.scheme, url.host, url.port

    def _new_rate_limiter(self) -> RateLimitTracker:
        return RateLimitTracker(
            buffer=settings.collection.rate_limit_buffer,
            fallback_wait=settings.collection.rate_limit_pause_seconds,
            clock=self._clock,
            sleep=self._sleep,
        )

    def close(self) -> None:
        """Close underlying HTTP sessions."""
        self._sync_client.close()

    def __enter__(self) -> "HTTPClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        use_cache: bool = True,
    ) -> httpx.Response:
        """Execute GET request with rate limiting and exponential backoff retry."""
        if self._sync_client.is_closed:
            raise RuntimeError("HTTPClient is closed")
        request = self._sync_client.build_request("GET", url, params=params, headers=headers)
        # Log the origin and path only, excluding credentials and query parameters.
        label = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        if use_cache and self.cache is not None:
            cached = self.cache.get(request)
            if cached is not None:
                logger.debug("HTTP GET cache hit: %s", label)
                return cached
        origin = self._origin(request.url)
        if origin not in self._rate_limiters:
            self._rate_limiters[origin] = (
                self.rate_limiter if not self._rate_limiters else self._new_rate_limiter()
            )
        rate_limiter = self._rate_limiters[origin]
        retry_dec = get_default_retry_decorator(
            max_attempts=self.max_retries,
            min_wait=self.backoff_factor,
            max_wait=self.max_backoff_seconds,
            wait_for_rate_limit=rate_limiter.seconds_until_ready,
            sleep=self._sleep,
        )

        @retry_dec
        def _request() -> httpx.Response:
            rate_limiter.check_and_wait_if_needed()
            response = self._sync_client.send(request)
            rate_limiter.update_from_headers(
                response.headers, rate_limited=is_rate_limited_response(response)
            )
            logger.debug("HTTP GET %s -> %d", label, response.status_code)
            response.raise_for_status()
            return response

        try:
            response = _request()
        except httpx.HTTPStatusError as e:
            logger.warning("HTTP GET failed: %s (status=%d)", label, e.response.status_code)
            if is_rate_limited_response(e.response):
                raise RateLimitExceededError(
                    f"Rate limit exhausted fetching {label}",
                    reset_timestamp=max(
                        rate_limiter.reset_epoch or 0.0, rate_limiter.retry_after_epoch or 0.0
                    )
                    or None,
                    status_code=e.response.status_code,
                    response_body=e.response.text,
                ) from e
            raise HTTPClientError(
                f"HTTP {e.response.status_code} error fetching {label}",
                status_code=e.response.status_code,
                response_body=e.response.text,
            ) from e
        except httpx.RequestError as e:
            logger.warning("HTTP GET failed: %s (%s)", label, type(e).__name__)
            raise HTTPClientError(f"Request failed for {label}: {type(e).__name__}") from e
        if use_cache and self.cache is not None:
            self.cache.store(request, response)
        return response

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        use_cache: bool = True,
    ) -> Any:
        """Convenience method returning parsed JSON."""
        response = self.get(url, params=params, headers=headers, use_cache=use_cache)
        try:
            return response.json()
        except ValueError as e:
            raise HTTPClientError(
                "Response did not contain valid JSON",
                status_code=response.status_code,
                response_body=response.text,
            ) from e
