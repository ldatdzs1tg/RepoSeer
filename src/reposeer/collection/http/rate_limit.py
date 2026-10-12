"""Rate limiting detection and state tracking."""

import logging
import math
import time
from collections.abc import Callable, Mapping
from datetime import UTC
from email.utils import parsedate_to_datetime

import httpx

logger = logging.getLogger("reposeer.http.rate_limit")


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Return the delay for either a Retry-After delta or an HTTP date."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            seconds = deadline.timestamp() - now
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def is_rate_limited_response(response: httpx.Response) -> bool:
    """Recognize 429 and GitHub's primary/secondary rate-limit 403 responses."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    headers = response.headers
    if headers.get("x-ratelimit-remaining", "").strip() == "0":
        return True
    if parse_retry_after(headers.get("retry-after"), time.time()) is not None:
        return True
    message = response.text[:1000].lower()
    return any(
        marker in message
        for marker in ("secondary rate limit", "rate limit exceeded", "abuse detection")
    )


class RateLimitTracker:
    """Tracks one origin's quota and cooldown, including plain Retry-After."""

    def __init__(
        self,
        buffer: int = 5,
        fallback_wait: float = 60.0,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        if buffer < 0 or fallback_wait < 0 or not math.isfinite(fallback_wait):
            raise ValueError("Rate limit buffer and fallback wait must be non-negative")
        self.buffer = buffer
        self.fallback_wait = fallback_wait
        self._clock = clock or time.time
        self._sleep = sleep or time.sleep
        self.remaining: int | None = None
        self.reset_epoch: float | None = None
        self.retry_after_epoch: float | None = None
        self._consecutive_limits = 0

    def update_from_headers(
        self, headers: Mapping[str, str], *, rate_limited: bool = False
    ) -> None:
        """Parse headers case-insensitively; malformed values cannot cause sleeps."""
        headers = httpx.Headers(headers)
        if "x-ratelimit-remaining" in headers:
            try:
                self.remaining = max(0, int(headers["x-ratelimit-remaining"]))
            except ValueError:
                self.remaining = None
        if "x-ratelimit-reset" in headers:
            try:
                reset = float(headers["x-ratelimit-reset"])
                self.reset_epoch = reset if math.isfinite(reset) else None
            except ValueError:
                self.reset_epoch = None

        now = self._clock()
        delay = parse_retry_after(headers.get("retry-after"), now)
        self.retry_after_epoch = now + delay if delay is not None else None
        self._consecutive_limits = self._consecutive_limits + 1 if rate_limited else 0
        if rate_limited and delay is None and self.seconds_until_ready() == 0:
            # GitHub secondary limits may omit both quota and Retry-After headers.
            self.retry_after_epoch = now + self.fallback_wait * (
                2 ** (self._consecutive_limits - 1)
            )

    def seconds_until_ready(self) -> float:
        """Return the remaining server-directed wait without sleeping."""
        now = self._clock()
        deadline = self.retry_after_epoch or 0.0
        if self.remaining is not None and self.remaining <= self.buffer:
            deadline = max(deadline, self.reset_epoch or 0.0)
        if self.reset_epoch is not None and self.reset_epoch <= now:
            self.remaining = None
            self.reset_epoch = None
        if self.retry_after_epoch is not None and self.retry_after_epoch <= now:
            self.retry_after_epoch = None
        return max(0.0, deadline - now)

    def check_and_wait_if_needed(self) -> None:
        """Pace requests before each attempt, then discard expired cooldowns."""
        wait_time = self.seconds_until_ready()
        if wait_time > 0:
            logger.warning("Waiting %.1f seconds for upstream rate limit", wait_time)
            self._sleep(wait_time)
            self.seconds_until_ready()
