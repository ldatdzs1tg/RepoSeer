"""Behavioral tests for the common HTTP client, using no external network."""

import logging

import httpx
import pytest

from reposeer.collection.http import HTTPClient, ResponseCache
from reposeer.config import settings
from reposeer.exceptions import HTTPClientError, RateLimitExceededError


def test_effective_headers_query_timeout_and_context_lifecycle():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    with HTTPClient(
        base_url="https://example.test/api/",
        headers={"Authorization": "Bearer example"},
        timeout=4.5,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.get_json("items", params={"page": 2}, headers={"Accept": "text/plain"}) == {
            "ok": True
        }
    request = requests[0]
    assert str(request.url) == "https://example.test/api/items?page=2"
    assert request.headers["user-agent"] == "RepoSeer/0.1.0"
    assert request.headers["authorization"] == "Bearer example"
    assert request.headers["accept"] == "text/plain"
    assert set(request.extensions["timeout"].values()) == {4.5}
    with pytest.raises(RuntimeError, match="closed"):
        client.get("items")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": float("inf")},
        {"max_retries": 0},
        {"max_retries": -1},
        {"max_retries": 1.5},
        {"backoff_factor": -1},
        {"max_backoff_seconds": float("nan")},
    ],
)
def test_invalid_options_are_not_silently_replaced_with_defaults(kwargs):
    with pytest.raises(ValueError):
        HTTPClient(**kwargs)


def test_constructor_headers_override_defaults_case_insensitively():
    def handler(request):
        assert request.headers["accept"] == "text/html"
        assert request.headers["user-agent"] == "custom-agent"
        return httpx.Response(200)

    with HTTPClient(
        headers={"accept": "text/html", "user-agent": "custom-agent"},
        transport=httpx.MockTransport(handler),
    ) as client:
        client.get("https://example.test")


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_transient_status_retries_with_bounded_backoff(status, fake_clock):
    responses = [status, status, 200]
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(responses.pop(0), json={"ok": True})

    with HTTPClient(
        max_retries=3,
        backoff_factor=2,
        max_backoff_seconds=3,
        transport=httpx.MockTransport(handler),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    ) as client:
        assert client.get_json("https://example.test/items") == {"ok": True}
    assert len(calls) == 3
    assert fake_clock.sleeps == [2, 3]


@pytest.mark.parametrize(
    "exception_type",
    [httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError],
)
def test_transient_network_failure_retries(exception_type, fake_clock):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise exception_type("temporary failure", request=request)
        return httpx.Response(200, text="ready")

    with HTTPClient(
        transport=httpx.MockTransport(handler),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    ) as client:
        assert client.get("https://example.test").text == "ready"
    assert len(calls) == 2
    assert fake_clock.sleeps == [settings.collection.backoff_factor]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_errors_fail_once_and_preserve_response(status, fake_clock):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="denied")

    with (
        HTTPClient(
            transport=httpx.MockTransport(handler),
            clock=fake_clock,
            sleep=fake_clock.sleep,
        ) as client,
        pytest.raises(HTTPClientError) as error,
    ):
        client.get("https://example.test/private")
    assert len(calls) == 1
    assert fake_clock.sleeps == []
    assert error.value.status_code == status
    assert error.value.response_body == "denied"
    assert isinstance(error.value.__cause__, httpx.HTTPStatusError)


@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_is_observed_without_quota_headers_or_double_sleep(status, fake_clock):
    attempts = []

    def handler(request):
        attempts.append(fake_clock())
        if len(attempts) == 1:
            return httpx.Response(status, headers={"Retry-After": "7"})
        return httpx.Response(200)

    with HTTPClient(
        max_backoff_seconds=2,
        transport=httpx.MockTransport(handler),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    ) as client:
        client.get("https://example.test/items")
    assert attempts[1] - attempts[0] == 7
    assert fake_clock.sleeps == [7]


@pytest.mark.parametrize("kind", ["primary", "secondary_header", "secondary_message"])
def test_github_rate_limited_403_is_retryable(kind, fake_clock, monkeypatch):
    monkeypatch.setattr(settings.collection, "rate_limit_pause_seconds", 20)
    headers = {
        "primary": {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(fake_clock() + 15)},
        "secondary_header": {"Retry-After": "8"},
        "secondary_message": {},
    }[kind]
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(403, headers=headers, json={"message": "secondary rate limit"})
        return httpx.Response(200, json={"ok": True})

    with HTTPClient(
        transport=httpx.MockTransport(handler),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    ) as client:
        assert client.get_json("https://api.github.com/repos/a/b") == {"ok": True}
    assert len(calls) == 2
    assert fake_clock.sleeps == [
        {"primary": 15, "secondary_header": 8, "secondary_message": 20}[kind]
    ]


def test_missing_rate_headers_use_increasing_configured_fallback(fake_clock, monkeypatch):
    monkeypatch.setattr(settings.collection, "rate_limit_pause_seconds", 20)
    statuses = iter([429, 429, 200])
    with HTTPClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(next(statuses))),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    ) as client:
        assert client.get("https://example.test").status_code == 200
    assert fake_clock.sleeps == [20, 40]


@pytest.mark.parametrize("status", [403, 429])
def test_exhausted_rate_limit_preserves_status_body_and_deadline(status, fake_clock):
    attempts = []

    def handler(request):
        attempts.append(request)
        return httpx.Response(status, headers={"Retry-After": "4"}, text="rate limit exceeded")

    with (
        HTTPClient(
            max_retries=2,
            transport=httpx.MockTransport(handler),
            clock=fake_clock,
            sleep=fake_clock.sleep,
        ) as client,
        pytest.raises(RateLimitExceededError) as error,
    ):
        client.get("https://example.test")
    assert len(attempts) == 2
    assert fake_clock.sleeps == [4]
    assert error.value.status_code == status
    assert error.value.response_body == "rate limit exceeded"
    assert error.value.reset_timestamp == fake_clock() + 4


def test_cooldown_is_scoped_to_origin(fake_clock):
    attempts = []

    def handler(request):
        attempts.append(request.url.host)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "10"})
        return httpx.Response(200)

    with HTTPClient(
        max_retries=1,
        transport=httpx.MockTransport(handler),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    ) as client:
        with pytest.raises(RateLimitExceededError):
            client.get("https://first.test")
        client.get("https://second.test")
        assert fake_clock.sleeps == []
        client.get("https://first.test")
    assert fake_clock.sleeps == [10]


def test_exhausted_timeout_is_wrapped_without_unbounded_retries(fake_clock):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectTimeout("timeout", request=request)

    with (
        HTTPClient(
            max_retries=2,
            transport=httpx.MockTransport(handler),
            clock=fake_clock,
            sleep=fake_clock.sleep,
        ) as client,
        pytest.raises(HTTPClientError) as error,
    ):
        client.get("https://example.test")
    assert len(calls) == 2
    assert isinstance(error.value.__cause__, httpx.ConnectTimeout)


def test_invalid_json_is_a_collection_error():
    with (
        HTTPClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not json"))
        ) as client,
        pytest.raises(HTTPClientError, match="valid JSON") as error,
    ):
        client.get_json("https://example.test")
    assert error.value.status_code == 200
    assert error.value.response_body == "not json"


def test_cache_hit_bypasses_network_and_cooldown_and_can_be_bypassed(tmp_path, fake_clock):
    calls = []

    def handler(request):
        calls.append(request)
        headers = (
            {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(fake_clock() + 10)}
            if len(calls) == 1
            else {}
        )
        return httpx.Response(200, json={"version": len(calls)}, headers=headers)

    with HTTPClient(
        cache=ResponseCache(tmp_path, clock=fake_clock),
        transport=httpx.MockTransport(handler),
        clock=fake_clock,
        sleep=fake_clock.sleep,
    ) as client:
        assert client.get_json("https://example.test") == {"version": 1}
        cached = client.get("https://example.test")
        assert cached.extensions["from_cache"] is True
        assert fake_clock.sleeps == []
        assert client.get_json("https://example.test", use_cache=False) == {"version": 2}
        assert client.get_json("https://example.test") == {"version": 1}
    assert len(calls) == 2


def test_cache_can_be_enabled_in_shared_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.collection, "cache_enabled", True)
    monkeypatch.setattr(settings.collection, "cache_dir", tmp_path)
    with HTTPClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="cached"))
    ) as client:
        client.get("https://example.test")
    with HTTPClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected network call"))
    ) as client:
        assert client.get("https://example.test").text == "cached"


def test_request_logs_do_not_expose_query_or_authentication(caplog, fake_clock):
    statuses = iter([503, 404])
    caplog.set_level(logging.DEBUG, logger="reposeer")
    with (
        HTTPClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(next(statuses))),
            clock=fake_clock,
            sleep=fake_clock.sleep,
        ) as client,
        pytest.raises(HTTPClientError) as error,
    ):
        client.get(
            "https://example.test/items?api_key=secret-query",
            headers={"Authorization": "secret-token"},
        )
    assert "Retrying HTTP request" in caplog.text
    assert "secret-query" not in caplog.text
    assert "secret-token" not in caplog.text
    assert "secret-query" not in str(error.value)
