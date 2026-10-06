"""Unit tests for retry predicate."""

import httpx

from reposeer.collection.http.retry import is_retryable_exception


def test_is_retryable_exception():
    # Retryable status codes
    req = httpx.Request("GET", "https://api.github.com")
    resp_500 = httpx.Response(500, request=req)
    resp_429 = httpx.Response(429, request=req)
    resp_404 = httpx.Response(404, request=req)
    resp_400 = httpx.Response(400, request=req)

    assert (
        is_retryable_exception(httpx.HTTPStatusError("500", request=req, response=resp_500)) is True
    )
    assert (
        is_retryable_exception(httpx.HTTPStatusError("429", request=req, response=resp_429)) is True
    )
    assert (
        is_retryable_exception(httpx.HTTPStatusError("404", request=req, response=resp_404))
        is False
    )
    assert (
        is_retryable_exception(httpx.HTTPStatusError("400", request=req, response=resp_400))
        is False
    )

    # Network exceptions
    assert is_retryable_exception(httpx.ConnectTimeout("timeout")) is True
