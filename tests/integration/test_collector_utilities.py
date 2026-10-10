"""Offline smoke test across GitHub and external collection boundaries."""

import httpx
import pytest

from reposeer.clients.external.hackernews import HackerNewsClient
from reposeer.clients.github import GitHubClient
from reposeer.collection import CollectionProgress, HTTPClient, ResponseCache, RunManager
from reposeer.collectors.external.crawler.fetcher import HTMLFetcher
from reposeer.collectors.github.repository import RepositoryCollector


def test_collector_utilities_smoke(tmp_path):
    """Exercise retries, TTL persistence, shared sessions, and run recovery together."""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "api.github.com":
            if len(calls) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(
                200,
                json={"id": 1, "full_name": "example/repo", "created_at": "2020-01-01T00:00:00Z"},
            )
        if request.url.host == "hn.algolia.com":
            return httpx.Response(
                200,
                json={
                    "hits": [
                        {"objectID": "1", "title": "Example", "url": "https://example.test/article"}
                    ]
                },
            )
        return httpx.Response(200, text="<html><body>Article</body></html>")

    manager = RunManager(tmp_path / "runs")
    context = manager.start_run("smoke")
    progress = CollectionProgress(context, manager)
    cache_dir = tmp_path / "cache"
    with (
        HTTPClient(
            cache=ResponseCache(cache_dir),
            backoff_factor=0,
            transport=httpx.MockTransport(handler),
        ) as http,
        GitHubClient(http_client=http) as github,
        HackerNewsClient(http_client=http, progress=progress) as discovery,
        HTMLFetcher(http, progress=progress) as fetcher,
    ):
        repository = RepositoryCollector(github, progress=progress).collect("example", "repo")
        candidate = discovery.search(repository.name, limit=1)[0]
        assert "Article" in fetcher.fetch(str(candidate.url))
    assert len(calls) == 4  # Two GitHub attempts, one discovery, one HTML fetch.

    resumed = RunManager(tmp_path / "runs").resume_run("smoke")
    assert resumed == context
    with (
        HTTPClient(
            cache=ResponseCache(cache_dir),
            transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected cache miss")),
        ) as http,
        HTMLFetcher(http, progress=CollectionProgress(resumed, manager)) as fetcher,
    ):
        assert "Article" in fetcher.fetch(str(candidate.url))
    final = manager.resume_run("smoke")
    assert final.collected_count == 4
    assert final.error_count == 0
    assert final.get_checkpoint("github.repository") == "example/repo"
    assert final.get_checkpoint("external.hackernews.query") == "repo"
    assert final.get_checkpoint("external.html.url") == "https://example.test/article"
