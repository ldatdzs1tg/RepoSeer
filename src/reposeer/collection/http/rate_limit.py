"""Rate limiting detection and state tracking."""

import logging
import time
from collections.abc import Mapping

logger = logging.getLogger("reposeer.http.rate_limit")


class RateLimitTracker:
    """Tracks rate limit state across API responses."""

    def __init__(self, buffer: int = 5):
        self.buffer = buffer
        self.remaining: int | None = None
        self.reset_epoch: float | None = None

    def update_from_headers(self, headers: Mapping[str, str]) -> None:
        """Parse standard GitHub and RFC rate limit headers."""
        # GitHub style
        if "x-ratelimit-remaining" in headers:
            try:
                self.remaining = int(headers["x-ratelimit-remaining"])
            except ValueError:
                pass
        if "x-ratelimit-reset" in headers:
            try:
                self.reset_epoch = float(headers["x-ratelimit-reset"])
            except ValueError:
                pass

        # Standard Retry-After
        if "retry-after" in headers:
            try:
                wait_sec = float(headers["retry-after"])
                self.reset_epoch = time.time() + wait_sec
            except ValueError:
                pass

    def check_and_wait_if_needed(self) -> None:
        """Wait if the rate limit is depleted or below buffer threshold."""
        if (
            self.remaining is not None
            and self.remaining <= self.buffer
            and self.reset_epoch is not None
        ):
            now = time.time()
            wait_time = max(0.0, self.reset_epoch - now) + 1.0
            if wait_time > 0:
                logger.warning(
                    "Rate limit threshold reached (remaining: %d). Sleeping for %.1f seconds...",
                    self.remaining,
                    wait_time,
                )
                time.sleep(wait_time)
