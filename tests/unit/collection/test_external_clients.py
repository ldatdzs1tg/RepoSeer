"""Unit tests for external discovery provider clients (T-016 Sub-Task 16.3)."""

from datetime import UTC, datetime

import httpx
import pytest

from reposeer.clients.external import (
    DevToClient,
    DiscoveryPageResult,
    HackerNewsClient,
    NewsAPIProvider,
    RSSProvider,
    StackExchangeClient,
)
from reposeer.collection import CollectionProgress, HTTPClient, RunManager
from reposeer.exceptions import HTTPClientError
from reposeer.schemas.external.candidate import ExternalCandidate


def test_hackernews_client_search_page_with_window(tmp_path):
    captured_requests = []

    def handler(request: httpx.Request):
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "objectID": "1001",
                        "title": "Flask 3.0 Released",
                        "url": "https://palletsprojects.com/blog/flask-3",
                        "points": 42,
                        "created_at_i": 1700000000,
                        "author": "davidism",
                        "num_comments": 15,
                        "story_text": "Announcing the release of Flask 3.0",
                    },
                    {
                        "objectID": "1002",
                        "title": "Ask HN: What is your favorite WSGI framework?",
                        "url": None,  # Ask HN post without external URL
                        "points": 10,
                        "created_at": "2023-11-15T00:00:00Z",
                        "author": "hnuser",
                        "num_comments": 25,
                    },
                ],
                "nbPages": 3,
                "nbHits": 50,
            },
        )

    manager = RunManager(tmp_path)
    progress = CollectionProgress(manager.start_run("hn-test"), manager)

    w_start = datetime(2023, 1, 1, 0, 0, 0, tzinfo=UTC)
    w_end = datetime(2023, 12, 31, 23, 59, 59, tzinfo=UTC)

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        client = HackerNewsClient(http_client=http, progress=progress)
        assert client.provider_name == "hackernews"
        assert client.supports_historical_window is True

        result: DiscoveryPageResult = client.search_page(
            "flask release",
            window_start=w_start,
            window_end=w_end,
            cursor="0",
            page_size=20,
        )

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.url.path == "/api/v1/search_by_date"
    assert req.url.params["query"] == "flask release"
    assert req.url.params["hitsPerPage"] == "20"
    assert req.url.params["page"] == "0"
    assert req.url.params["tags"] == "story"

    numeric_filters = req.url.params["numericFilters"]
    assert f"created_at_i>={int(w_start.timestamp())}" in numeric_filters
    assert f"created_at_i<={int(w_end.timestamp())}" in numeric_filters

    assert len(result.candidates) == 2
    assert result.page_number == 1
    assert result.has_more is True
    assert result.next_cursor == "1"
    assert result.total_hits_reported == 50

    # First candidate: explicit URL
    c1 = result.candidates[0]
    assert c1.title == "Flask 3.0 Released"
    assert c1.url == "https://palletsprojects.com/blog/flask-3"
    assert c1.source == "hackernews"
    assert c1.source_item_id == "hn:1001"
    assert c1.source_item_url == "https://news.ycombinator.com/item?id=1001"
    assert c1.native_payload["objectID"] == "1001"
    assert c1.score == 42.0
    assert c1.author == "davidism"
    assert c1.comment_count == 15
    assert c1.snippet == "Announcing the release of Flask 3.0"
    assert c1.publication_date == datetime.fromtimestamp(1700000000, tz=UTC)

    # Second candidate: Ask HN fallback URL
    c2 = result.candidates[1]
    assert c2.title == "Ask HN: What is your favorite WSGI framework?"
    assert c2.url == "https://news.ycombinator.com/item?id=1002"
    assert c2.source_item_id == "hn:1002"
    assert c2.publication_date == datetime(2023, 11, 15, 0, 0, 0, tzinfo=UTC)

    assert result.observed_from is not None
    assert result.observed_to is not None
    assert result.observed_from <= result.observed_to

    # Verify progress recorded
    assert progress.context.get_checkpoint("external.hackernews.query") == "flask release"


def test_hackernews_client_search_page_without_window():
    captured_requests = []

    def handler(request: httpx.Request):
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={"hits": [], "nbPages": 1, "nbHits": 0},
        )

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        client = HackerNewsClient(http_client=http)
        result = client.search_page("pallets/flask", page_size=10)

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.url.path == "/api/v1/search"
    assert "numericFilters" not in req.url.params
    assert result.has_more is False
    assert result.next_cursor is None


def test_stackexchange_client_search_page(tmp_path):
    captured_requests = []

    def handler(request: httpx.Request):
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "question_id": 98765,
                        "title": "How to use &quot;Flask&quot; with &lt;FastAPI&gt;?",
                        "link": "https://stackoverflow.com/questions/98765/flask-fastapi",
                        "creation_date": 1690000000,
                        "score": 12,
                        "answer_count": 3,
                        "tags": ["python", "flask", "fastapi"],
                        "owner": {"display_name": "py_coder"},
                    }
                ],
                "has_more": True,
                "total": 450,
            },
        )

    manager = RunManager(tmp_path)
    progress = CollectionProgress(manager.start_run("se-test"), manager)

    w_start = datetime(2023, 1, 1, tzinfo=UTC)
    w_end = datetime(2023, 12, 31, tzinfo=UTC)

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        client = StackExchangeClient(
            http_client=http,
            progress=progress,
            api_key="synthetic_se_key",
        )
        assert client.provider_name == "stackexchange"
        assert client.supports_historical_window is True

        result: DiscoveryPageResult = client.search_page(
            "flask python",
            window_start=w_start,
            window_end=w_end,
            cursor="2",
            page_size=50,
        )

    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.url.path == "/2.3/search/advanced"
    assert req.url.params["site"] == "stackoverflow"
    assert req.url.params["q"] == "flask python"
    assert req.url.params["page"] == "2"
    assert req.url.params["pagesize"] == "50"
    assert req.url.params["sort"] == "creation"
    assert req.url.params["order"] == "desc"
    assert req.url.params["fromdate"] == str(int(w_start.timestamp()))
    assert req.url.params["todate"] == str(int(w_end.timestamp()))
    assert "key" not in req.url.params
    assert req.headers["Authorization"] == "Bearer synthetic_se_key"

    assert len(result.candidates) == 1
    assert result.page_number == 2
    assert result.has_more is True
    assert result.next_cursor == "3"
    assert result.total_hits_reported == 450

    c = result.candidates[0]
    # Verify unescaped HTML title
    assert c.title == 'How to use "Flask" with <FastAPI>?'
    assert c.url == "https://stackoverflow.com/questions/98765/flask-fastapi"
    assert c.source == "stackexchange"
    assert c.source_item_id == "se:98765"
    assert c.score == 12.0
    assert c.comment_count == 3
    assert c.author == "py_coder"
    assert c.snippet == "python, flask, fastapi"
    assert c.publication_date == datetime.fromtimestamp(1690000000, tz=UTC)

    assert progress.context.get_checkpoint("external.stackexchange.query") == "flask python"


def test_stackexchange_client_api_error():
    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "error_id": 502,
                "error_message": "too many requests from this IP",
                "error_name": "throttle_violation",
            },
        )

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        client = StackExchangeClient(http_client=http)
        with pytest.raises(HTTPClientError, match="throttle_violation"):
            client.search_page("flask")


def test_devto_client_search_page_with_window_cutoff(tmp_path):
    captured_requests = []

    def handler(request: httpx.Request):
        captured_requests.append(request)
        return httpx.Response(
            200,
            json=[
                # Article 1: Future article after window_end (cutoff boundary)
                {
                    "id": 201,
                    "title": "Future of Flask in 2025",
                    "url": "https://dev.to/author/future-of-flask",
                    "published_timestamp": "2025-01-10T12:00:00Z",
                    "positive_reactions_count": 25,
                    "comments_count": 4,
                    "description": "Predictions for Python frameworks in 2025",
                    "user": {"name": "Alice Dev"},
                },
                # Article 2: Inside the window
                {
                    "id": 202,
                    "title": "Building REST APIs with Flask",
                    "url": "https://dev.to/author/building-rest-apis-with-flask",
                    "published_at": "2024-06-15T10:00:00Z",
                    "positive_reactions_count": 45,
                    "comments_count": 8,
                    "description": "Step by step Flask guide",
                    "user": {"username": "bob_coder"},
                },
                # Article 3: Past window_start (too old)
                {
                    "id": 203,
                    "title": "Old Flask 0.12 Tips",
                    "url": "https://dev.to/author/old-flask-tips",
                    "published_timestamp": "2022-01-01T00:00:00Z",
                    "positive_reactions_count": 2,
                    "comments_count": 1,
                    "description": "Historical notes on legacy Flask",
                    "user": {"name": "Charlie"},
                },
            ],
        )

    manager = RunManager(tmp_path)
    progress = CollectionProgress(manager.start_run("devto-test"), manager)

    w_start = datetime(2024, 1, 1, tzinfo=UTC)
    w_end = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        client = DevToClient(http_client=http, progress=progress)
        assert client.provider_name == "devto"
        assert client.supports_historical_window is False

        result: DiscoveryPageResult = client.search_page(
            "flask python",
            window_start=w_start,
            window_end=w_end,
            cursor="1",
            page_size=10,
        )

    assert len(captured_requests) == 1
    assert captured_requests[0].url.path == "/api/articles/search"
    assert captured_requests[0].url.params["q"] == "flask python"
    assert captured_requests[0].url.params["per_page"] == "10"
    assert captured_requests[0].url.params["page"] == "1"

    # Only Article 2 should survive both cutoff (future) and start (past) filtering
    assert len(result.candidates) == 1
    c = result.candidates[0]
    assert result.native_items_observed == 3
    assert c.title == "Building REST APIs with Flask"
    assert c.source == "devto"
    assert c.source_item_id == "devto:202"
    assert c.url == "https://dev.to/author/building-rest-apis-with-flask"
    assert c.score == 45.0
    assert c.comment_count == 8
    assert c.author == "bob_coder"
    assert c.snippet == "Step by step Flask guide"
    assert c.publication_date == datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)

    # The native page is shorter than per_page, so pagination is exhausted.
    assert result.has_more is False
    assert result.next_cursor is None

    assert progress.context.get_checkpoint("external.devto.query") == "flask python"


def test_newsapi_pagination_errors_and_zero_results():
    requests = []
    payload = {"status": "ok", "totalResults": 3, "articles": [{
        "title": "Flask release", "url": "https://example.com/flask", "description": "Python package",
        "source": {"name": "Example"}, "author": "Author", "publishedAt": "2025-01-01T00:00:00Z"}]}

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        news = NewsAPIProvider("synthetic-news", http_client=http)
        result = news.search_page("flask", page_size=1)
        assert result.has_more and result.next_cursor == "2"
        candidate = result.candidates[0]
        assert candidate.source == "news" and candidate.source_item_id.startswith("news:")
        assert candidate.publication_date == datetime(2025, 1, 1, tzinfo=UTC)
        assert candidate.snippet == "Python package"
        assert requests[0].headers["X-Api-Key"] == "synthetic-news"
        assert "synthetic-news" not in str(requests[0].url)
        assert requests[0].url.params["q"] == "flask"
        payload.update(totalResults=0, articles=[])
        assert news.search_page("flask").candidates == []
        payload.update(status="error", code="apiKeyExhausted", message="synthetic-news")
        with pytest.raises(HTTPClientError, match="quota_exhausted") as error:
            news.search_page("flask")
        assert "synthetic-news" not in str(error.value)
        before = len(requests)
        with pytest.raises(HTTPClientError, match="missing_configuration"):
            NewsAPIProvider(http_client=http).search_page("flask")
        assert len(requests) == before


@pytest.mark.parametrize("body", [
    b'<rss version="2.0"><channel><item><title>Flask Python release</title><link>https://example.com/flask</link><guid>one</guid><pubDate>Wed, 01 Jan 2025 00:00:00 GMT</pubDate><description>Flask package</description></item></channel></rss>',
    b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Flask Python release</title><link href="https://example.com/flask"/><id>one</id><published>2025-01-01T00:00:00Z</published><summary>Flask package</summary></entry></feed>',
])
def test_rss_real_feeds_cached_per_invocation(body):
    from reposeer.collectors.external.crawler.safety import URLPolicy

    requests = []

    def handler(request):
        requests.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(200, content=body)

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        rss = RSSProvider(["https://feeds.example/python"], http_client=http,
                          policy=URLPolicy(lambda host: ["93.184.216.34"]))
        first = rss.search_page("flask python")
        assert first.candidates[0].publication_date == datetime(2025, 1, 1, tzinfo=UTC)
        assert first.candidates[0].native_payload["summary_only"] is True
        assert not first.has_more
        assert rss.search_page("unrelated").candidates == []
        assert rss.search_page("flask").candidates[0].discovered_at == first.candidates[0].discovered_at
        assert len(requests) == 1
        assert rss.supports_historical_window is False


def test_rss_rejects_private_feeds_entities_and_oversized_responses():
    from reposeer.clients.external.rss import parse_feed
    from reposeer.collectors.external.crawler.safety import URLPolicy

    for url in ("http://example.com/rss", "https://127.0.0.1/rss", "https://169.254.169.254/rss"):
        with pytest.raises(ValueError):
            RSSProvider([url])
    with pytest.raises(ValueError, match="unsafe_feed_xml"):
        parse_feed(b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss>&x;</rss>')
    with HTTPClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 2_000_001))) as http:
        rss = RSSProvider(["https://feeds.example/python"], http_client=http,
                          policy=URLPolicy(lambda host: ["93.184.216.34"]))
        with pytest.raises(HTTPClientError, match="byte limit"):
            rss.search_page("flask")


def test_preflight_blocks_all_network_and_keeps_secrets_private(capsys):
    from reposeer.collection.provider_preflight import check_providers
    from reposeer.config import Settings

    config = Settings(_env_file=None, GITHUB_TOKEN="synthetic-github-secret", DEVTO_API_KEY=" ",
                      STACKEXCHANGE_API_KEY="changeme", NEWS_API_KEY="synthetic-news-secret",
                      REPOSEER_RSS_FEEDS="https://127.0.0.1/feed")

    def forbidden(request):
        pytest.fail("static validation must precede every request")

    with HTTPClient(transport=httpx.MockTransport(forbidden)) as http:
        result = check_providers(configuration=config, http_client=http)
    assert not result["ready"]
    serialized = str(result) + capsys.readouterr().out
    assert "synthetic-github-secret" not in serialized and "synthetic-news-secret" not in serialized
    assert {item["provider"]: item["status"] for item in result["providers"]}["rss"] == "invalid_feed"


def test_live_preflight_requires_authenticated_stackexchange_quota(monkeypatch):
    import reposeer.collection.provider_preflight as preflight
    from reposeer.collectors.external.crawler.safety import URLPolicy
    from reposeer.config import Settings

    config = Settings(_env_file=None, GITHUB_TOKEN="unit-gh", DEVTO_API_KEY="unit-dev",
                      STACKEXCHANGE_API_KEY="unit-stack", NEWS_API_KEY="unit-news",
                      REPOSEER_RSS_FEEDS="https://feeds.example/rss")
    quota = [300]
    limited = [False]
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"id": 1, "login": "user"})
        if request.url.host == "dev.to":
            return httpx.Response(200, json={"id": 1, "username": "user"})
        if request.url.host == "api.stackexchange.com":
            if limited[0]:
                return httpx.Response(429)
            assert request.headers["Authorization"] == "Bearer unit-stack"
            assert "key" not in request.url.params
            return httpx.Response(200, json={"items": [{}], "quota_max": quota[0], "quota_remaining": 10})
        if request.url.host == "newsapi.org":
            return httpx.Response(200, json={"status": "ok", "articles": [], "totalResults": 0})
        if request.url.host == "feeds.example":
            return httpx.Response(200, content=b'<rss version="2.0"><channel/></rss>')
        return httpx.Response(200, json={"hits": [], "nbPages": 0})

    monkeypatch.setattr(preflight, "RSSProvider", lambda feeds, http_client:
                        RSSProvider(feeds, http_client=http_client, policy=URLPolicy(lambda host: ["93.184.216.34"])))
    with HTTPClient(transport=httpx.MockTransport(handler), max_retries=1) as http:
        blocked = preflight.check_providers(configuration=config, http_client=http)
        assert not blocked["ready"] and blocked["configuration_valid"]
        assert "stackexchange" in blocked["deferred_external_sources"]
        assert "devto" in blocked["available_external_sources"] and "news" in blocked["available_external_sources"]
        quota[0] = 10000
        assert preflight.check_providers(configuration=config, http_client=http)["ready"]
        requests.clear()
        assert preflight.check_providers(configuration=config, http_client=http, include_github=False)["ready"]
        assert all(request.url.host != "api.github.com" for request in requests)

        limited[0] = True
        result = preflight.check_providers(configuration=config, http_client=http,
                                          include_github=False, sources=["stackexchange"])
        assert result["configuration_valid"] and not result["ready"]
        assert result["provider_status"]["stackexchange"]["retry_after"] is None
        assert result["provider_status"]["stackexchange"]["status"] == "quota_exhausted"


def test_providers_backward_compatible_search():
    def hn_handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "hits": [
                    {"objectID": "1", "title": "HN 1", "url": "https://example.com/1"},
                    {"objectID": "2", "title": "HN 2", "url": "https://example.com/2"},
                ],
                "nbPages": 1,
            },
        )

    def se_handler(request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "items": [
                    {"question_id": 1, "title": "SE 1", "link": "https://example.com/se1"},
                ],
                "has_more": False,
            },
        )

    def devto_handler(request: httpx.Request):
        return httpx.Response(
            200,
            json=[
                {"id": 1, "title": "DevTo 1", "url": "https://example.com/dev1"},
            ],
        )

    with HTTPClient(transport=httpx.MockTransport(hn_handler)) as http:
        client = HackerNewsClient(http_client=http)
        items = client.search("test", limit=5)
        assert len(items) == 2
        assert isinstance(items[0], ExternalCandidate)

    with HTTPClient(transport=httpx.MockTransport(se_handler)) as http:
        client = StackExchangeClient(http_client=http)
        items = client.search("test", limit=5)
        assert len(items) == 1
        assert isinstance(items[0], ExternalCandidate)

    with HTTPClient(transport=httpx.MockTransport(devto_handler)) as http:
        client = DevToClient(http_client=http)
        items = client.search("test", limit=5)
        assert len(items) == 1
        assert isinstance(items[0], ExternalCandidate)


def test_devto_old_relevance_hit_does_not_end_pagination():
    requests = []

    def handler(request):
        requests.append(request)
        first_page = request.url.params["page"] == "1"
        return httpx.Response(200, json=[{
            "id": 1 if first_page else 2, "title": "DEV search hit",
            "url": "https://dev.to/author/story",
            "published_timestamp": "2020-01-01T00:00:00Z" if first_page else "2024-06-01T00:00:00Z",
        }])

    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        client = DevToClient(http_client=http)
        first = client.search_page("flask", window_start=datetime(2024, 1, 1, tzinfo=UTC), page_size=1)
        assert first.candidates == [] and first.has_more and first.next_cursor == "2"
        second = client.search_page("flask", window_start=datetime(2024, 1, 1, tzinfo=UTC),
                                    cursor=first.next_cursor, page_size=1)
    assert second.candidates[0].source_item_id == "devto:2"
    assert requests[0].url.path == "/api/articles/search"
    assert requests[1].url.params["q"] == "flask" and requests[1].url.params["page"] == "2"
    assert not client.supports_historical_window


@pytest.mark.parametrize("provider", ["news", "devto", "stackexchange"])
def test_authenticated_provider_rejects_redirect_before_forwarding_key(provider):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://other.example/steal"})

    classes = {"news": NewsAPIProvider, "devto": DevToClient, "stackexchange": StackExchangeClient}
    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        client = classes[provider](api_key="synthetic-private-key", http_client=http)
        with pytest.raises(HTTPClientError):
            client.search_page("flask")
    assert len(calls) == 1 and calls[0].url.host != "other.example"


def test_stackexchange_json_backoff_paces_subsequent_requests():
    now, waits = [1000.0], []

    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds

    with HTTPClient(clock=lambda: now[0], sleep=sleep, transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"items": [], "has_more": False, "backoff": 2}))) as http:
        provider = StackExchangeClient(http_client=http)
        provider.search_page("flask")
        provider.search_page("flask release")
        assert waits == [2.0] and http.requests_attempted == 2
