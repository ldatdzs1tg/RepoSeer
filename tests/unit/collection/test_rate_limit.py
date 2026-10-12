"""Header parsing and cooldown boundary tests."""

from datetime import UTC, datetime
from email.utils import format_datetime

import pytest

from reposeer.collection.http.rate_limit import RateLimitTracker, parse_retry_after


def test_http_date_retry_after_and_mixed_case_headers(fake_clock):
    tracker = RateLimitTracker(clock=fake_clock, sleep=fake_clock.sleep)
    deadline = format_datetime(datetime.fromtimestamp(fake_clock() + 12, UTC), usegmt=True)
    tracker.update_from_headers({"Retry-After": deadline})
    tracker.check_and_wait_if_needed()
    tracker.check_and_wait_if_needed()
    assert fake_clock.sleeps == [12]


def test_quota_buffer_paces_next_request_and_expired_reset_does_not_sleep(fake_clock):
    tracker = RateLimitTracker(buffer=5, clock=fake_clock, sleep=fake_clock.sleep)
    tracker.update_from_headers(
        {"X-RateLimit-Remaining": "4", "X-RateLimit-Reset": str(fake_clock() + 5)}
    )
    tracker.check_and_wait_if_needed()
    tracker.check_and_wait_if_needed()
    assert fake_clock.sleeps == [5]
    assert tracker.remaining is None
    assert tracker.reset_epoch is None


@pytest.mark.parametrize("value", ["garbage", "nan", "inf", "-inf", "", None])
def test_malformed_retry_after_is_ignored(value, fake_clock):
    assert parse_retry_after(value, fake_clock()) is None


@pytest.mark.parametrize("value", ["-1", "0", "Wed, 21 Oct 2015 07:28:00 GMT"])
def test_past_retry_after_requires_no_wait(value, fake_clock):
    assert parse_retry_after(value, fake_clock()) == 0


def test_malformed_quota_values_cannot_cause_waits(fake_clock):
    tracker = RateLimitTracker(clock=fake_clock, sleep=fake_clock.sleep)
    tracker.update_from_headers({"X-RateLimit-Remaining": "unknown", "X-RateLimit-Reset": "inf"})
    tracker.check_and_wait_if_needed()
    assert fake_clock.sleeps == []
