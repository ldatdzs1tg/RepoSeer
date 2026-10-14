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
    assert req.url.params["key"] == "synthetic_se_key"

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
    assert captured_requests[0].url.path == "/api/articles"
    assert captured_requests[0].url.params["q"] == "flask python"
    assert captured_requests[0].url.params["per_page"] == "10"
    assert captured_requests[0].url.params["page"] == "1"

    # Only Article 2 should survive both cutoff (future) and start (past) filtering
    assert len(result.candidates) == 1
    c = result.candidates[0]
    assert c.title == "Building REST APIs with Flask"
    assert c.source == "devto"
    assert c.source_item_id == "devto:202"
    assert c.url == "https://dev.to/author/building-rest-apis-with-flask"
    assert c.score == 45.0
    assert c.comment_count == 8
    assert c.author == "bob_coder"
    assert c.snippet == "Step by step Flask guide"
    assert c.publication_date == datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)

    # Reached past window_start, so has_more must be False
    assert result.has_more is False
    assert result.next_cursor is None

    assert progress.context.get_checkpoint("external.devto.query") == "flask python"


def test_rss_and_newsapi_providers():
    rss = RSSProvider()
    assert rss.provider_name == "rss"
    assert rss.supports_historical_window is False
    res_rss = rss.search_page("query")
    assert res_rss.candidates == []
    assert res_rss.has_more is False
    assert rss.search("query") == []

    news = NewsAPIProvider(api_key="test_key")
    assert news.provider_name == "news"
    res_news = news.search_page("query")
    assert res_news.candidates == []
    assert res_news.has_more is False
    assert news.search("query") == []


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
