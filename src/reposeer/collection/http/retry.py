"""Tenacity-based retry policy according to T-012 specification."""

import logging

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("reposeer.http.retry")

# HTTP status codes eligible for retry
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def is_retryable_exception(exc: BaseException) -> bool:
    """Determine whether an exception should trigger a retry.

    Retry on transient network issues and 5xx / 429 status codes.
    Fail fast on 400, 401, 404.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in RETRYABLE_STATUS_CODES
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
        ),
    ):
        return True
    return False


def get_default_retry_decorator(
    max_attempts: int = 3, min_wait: float = 1.0, max_wait: float = 10.0
):
    """Factory creating a tenacity retry decorator configured for HTTP requests."""
    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=min_wait, max=max_wait),
        retry=retry_if_exception(is_retryable_exception),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
