"""Shared, bounded retry policy for transient HTTP failures."""

import logging
import math
import time
from collections.abc import Callable

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from reposeer.collection.http.rate_limit import is_rate_limited_response

logger = logging.getLogger("reposeer.http.retry")

# HTTP status codes eligible for retry
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def is_retryable_exception(exc: BaseException) -> bool:
    """Determine whether an exception should trigger a retry.

    Retry on transient network issues and 5xx / 429 status codes.
    Fail fast on 400, 401, 404.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS_CODES or is_rate_limited_response(
            exc.response
        )
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    return False


def get_default_retry_decorator(
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 10.0,
    *,
    wait_for_rate_limit: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
):
    """Wait for the larger of exponential backoff and the server's cooldown."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if any(not math.isfinite(value) or value < 0 for value in (min_wait, max_wait)):
        raise ValueError("Retry waits must be finite and non-negative")
    exponential_wait = wait_exponential(multiplier=min_wait, max=max_wait)

    def wait(retry_state: RetryCallState) -> float:
        server_wait = wait_for_rate_limit() if wait_for_rate_limit is not None else 0.0
        return max(exponential_wait(retry_state), server_wait)

    def log_retry(retry_state: RetryCallState) -> None:
        # Exception strings from HTTPX contain URLs (possibly API keys in queries).
        exception = retry_state.outcome.exception() if retry_state.outcome else None
        status = (
            exception.response.status_code if isinstance(exception, httpx.HTTPStatusError) else None
        )
        logger.warning(
            "Retrying HTTP request after attempt %d/%d (%s, status=%s); waiting %.2fs",
            retry_state.attempt_number,
            max_attempts,
            type(exception).__name__,
            status,
            retry_state.next_action.sleep if retry_state.next_action else 0.0,
        )

    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait,
        retry=retry_if_exception(is_retryable_exception),
        before_sleep=log_retry,
        sleep=sleep or time.sleep,
    )
