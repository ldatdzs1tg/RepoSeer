"""External providers and crawlers reuse the common HTTP layer."""

import httpx

from reposeer.clients.external.hackernews import HackerNewsClient
from reposeer.collection import CollectionProgress, HTTPClient, ResponseCache, RunManager
from reposeer.collectors.external.crawler.fetcher import HTMLFetcher


def test_external_discovery_and_crawler_share_http_cache_and_progress(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "hn.algolia.com":
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {
                            "objectID": "42",
                            "title": "Example",
                            "url": "https://example.test/article",
                            "points": 5,
                        }
                    ]
                },
            )
        return httpx.Response(200, text="<html>Example article</html>")

    manager = RunManager(tmp_path / "runs")
    progress = CollectionProgress(manager.start_run("external"), manager)
    with HTTPClient(
        cache=ResponseCache(tmp_path / "cache"), transport=httpx.MockTransport(handler)
    ) as http:
        with HackerNewsClient(http_client=http, progress=progress) as discovery:
            candidates = discovery.search("example", limit=1)
        with HTMLFetcher(http, progress=progress) as fetcher:
            assert fetcher.fetch(str(candidates[0].url)) == "<html>Example article</html>"
            assert fetcher.fetch(str(candidates[0].url)) == "<html>Example article</html>"
        assert http.get(
            "https://example.test/article", headers={"Accept": "text/html,application/xhtml+xml"}
        ).extensions["from_cache"]
    assert len(requests) == 2
    assert requests[0].url.path == "/api/v1/search"
    assert requests[0].url.params["query"] == "example"
    assert requests[0].headers["accept"] == "application/json"
    assert requests[1].headers["accept"] == "text/html,application/xhtml+xml"
    restored = manager.resume_run("external")
    assert restored.collected_count == 3
    assert restored.error_count == 0
    assert restored.get_checkpoint("external.html.url") == "https://example.test/article"


def test_external_failure_is_logged_and_does_not_advance_checkpoint(tmp_path, caplog):
    manager = RunManager(tmp_path)
    progress = CollectionProgress(manager.start_run("external-errors"), manager)
    with (
        HTTPClient(
            max_retries=1, transport=httpx.MockTransport(lambda request: httpx.Response(404))
        ) as http,
        HTMLFetcher(http, progress=progress) as fetcher,
        HackerNewsClient(http_client=http, progress=progress) as discovery,
    ):
        assert fetcher.fetch("https://example.test/missing") is None
        assert discovery.search("example") == []
    restored = manager.resume_run("external-errors")
    assert restored.error_count == 2
    assert restored.collected_count == 0
    assert restored.checkpoint_state == {}
    assert "Failed to collect data" in caplog.text
