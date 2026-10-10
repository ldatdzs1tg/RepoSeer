"""Persistent cache behavior, isolation, and failure recovery."""

import gzip
import json

import httpx
import pytest

from reposeer.collection.http import HTTPClient, ResponseCache


def test_cache_survives_new_instance_and_preserves_bytes_headers_and_ttl(tmp_path, fake_clock):
    request = httpx.Request("GET", "https://example.test/items")
    content = "Tiếng Việt".encode()
    response = httpx.Response(
        200,
        content=content,
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Link": '<https://example.test/items?page=2>; rel="next"',
        },
    )
    ResponseCache(tmp_path, ttl_seconds=10, clock=fake_clock).store(request, response)
    restored = ResponseCache(tmp_path, clock=fake_clock)
    cached = restored.get(request)
    assert cached.content == content
    assert cached.text == "Tiếng Việt"
    assert cached.headers["link"] == response.headers["link"]
    assert cached.request is request
    assert cached.extensions["from_cache"] is True
    fake_clock.sleep(10)
    assert restored.get(request) is None


@pytest.mark.parametrize(
    "variant",
    [
        httpx.Request(
            "GET",
            "https://example.test/items?page=2",
            headers={"Accept": "application/json", "Authorization": "Bearer first"},
        ),
        httpx.Request(
            "GET",
            "https://example.test/items?page=1",
            headers={"Accept": "text/html", "Authorization": "Bearer first"},
        ),
        httpx.Request(
            "GET",
            "https://example.test/items?page=1",
            headers={"Accept": "application/json", "Authorization": "Bearer second"},
        ),
        httpx.Request(
            "GET",
            "https://other.test/items?page=1",
            headers={"Accept": "application/json", "Authorization": "Bearer first"},
        ),
    ],
)
def test_cache_separates_queries_representations_credentials_and_origins(tmp_path, variant):
    request = httpx.Request(
        "GET",
        "https://example.test/items?page=1",
        headers={"Accept": "application/json", "Authorization": "Bearer first"},
    )
    cache = ResponseCache(tmp_path)
    cache.store(request, httpx.Response(200, json={"private": True}))
    assert cache.get(variant) is None
    persisted = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    assert "Bearer first" not in persisted
    assert "example.test" not in persisted


@pytest.mark.parametrize(
    "status,headers",
    [
        (404, {}),
        (429, {}),
        (503, {}),
        (200, {"Cache-Control": "no-store"}),
        (200, {"Cache-Control": "No-Cache"}),
        (200, {"Set-Cookie": "session=private"}),
        (200, {"Vary": "*"}),
        (200, {"Vary": "Accept, *"}),
    ],
)
def test_errors_and_uncacheable_responses_are_not_saved(tmp_path, status, headers):
    cache = ResponseCache(tmp_path)
    request = httpx.Request("GET", "https://example.test")
    cache.store(request, httpx.Response(status, headers=headers))
    assert cache.get(request) is None
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("method,headers", [("POST", {}), ("GET", {"Cache-Control": "no-store"})])
def test_non_get_and_explicit_request_cache_control_bypass_cache(tmp_path, method, headers):
    cache = ResponseCache(tmp_path)
    request = httpx.Request(method, "https://example.test", headers=headers)
    cache.store(request, httpx.Response(200))
    assert cache.get(request) is None
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize(
    "damage", ["invalid_json", "invalid_base64", "invalid_headers", "invalid_expiry"]
)
def test_damaged_cache_is_a_miss_and_is_replaced_by_fresh_response(tmp_path, damage, caplog):
    request = httpx.Request("GET", "https://example.test")
    cache = ResponseCache(tmp_path)
    cache.store(request, httpx.Response(200, text="old"))
    path = next(tmp_path.glob("*.json"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    if damage == "invalid_json":
        path.write_text("{truncated", encoding="utf-8")
    else:
        field, value = {
            "invalid_base64": ("content", "%%%"),
            "invalid_headers": ("headers", 42),
            "invalid_expiry": ("expires_at", "nan"),
        }[damage]
        entry[field] = value
        path.write_text(json.dumps(entry), encoding="utf-8")
    assert cache.get(request) is None
    assert "Ignoring unreadable HTTP cache entry" in caplog.text
    cache.store(request, httpx.Response(200, text="fresh"))
    assert cache.get(request).text == "fresh"


def test_unwritable_cache_does_not_fail_collection(tmp_path, caplog):
    blocking_file = tmp_path / "file"
    blocking_file.write_text("not a directory", encoding="utf-8")
    with HTTPClient(
        cache=ResponseCache(blocking_file),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="ok")),
    ) as client:
        assert client.get("https://example.test").text == "ok"
    assert "Could not persist HTTP cache entry" in caplog.text


def test_compressed_response_can_be_loaded_from_cache_without_double_decoding(tmp_path):
    payload = b"<html>article</html>"
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, content=gzip.compress(payload), headers={"Content-Encoding": "gzip"}
        )

    with HTTPClient(
        cache=ResponseCache(tmp_path), transport=httpx.MockTransport(handler)
    ) as client:
        assert client.get("https://example.test").content == payload
        assert client.get("https://example.test").content == payload
    assert len(calls) == 1


@pytest.mark.parametrize("ttl", [0, -1, float("inf"), float("nan")])
def test_invalid_ttl_is_rejected(tmp_path, ttl):
    with pytest.raises(ValueError):
        ResponseCache(tmp_path, ttl_seconds=ttl)
