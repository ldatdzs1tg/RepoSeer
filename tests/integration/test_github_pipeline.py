"""GitHub collectors use the shared HTTP policy and progress utilities."""

import httpx
import pytest

from reposeer.clients.github import GitHubClient
from reposeer.collection import CollectionProgress, HTTPClient, RunManager
from reposeer.collectors.github.repository import RepositoryCollector
from reposeer.exceptions import HTTPClientError


def test_github_collector_reuses_injected_http_and_persists_progress(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": 123,
                "full_name": "example/repo",
                "stargazers_count": 7,
                "created_at": "2020-01-01T00:00:00Z",
            },
        )

    manager = RunManager(tmp_path)
    progress = CollectionProgress(manager.start_run("github"), manager)
    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        with GitHubClient(token="test-token", http_client=http) as github:
            record = RepositoryCollector(github, progress=progress).collect("example", "repo")
        # A provider must not close a shared, caller-owned session.
        assert http.get("https://example.test/ping").status_code == 200
    assert record.full_name == "example/repo"
    assert record.stars == 7
    assert str(requests[0].url) == "https://api.github.com/repos/example/repo"
    assert requests[0].headers["authorization"] == "Bearer test-token"
    assert requests[0].headers["accept"] == "application/vnd.github+json"
    assert "authorization" not in requests[1].headers
    restored = manager.resume_run("github")
    assert restored.get_checkpoint("github.repository") == "example/repo"
    assert restored.collected_count == 1


def test_github_errors_are_counted_and_optional_files_only_hide_404(tmp_path):
    statuses = iter([404, 403, 401])
    manager = RunManager(tmp_path)
    progress = CollectionProgress(manager.start_run("github-errors"), manager)
    with (
        HTTPClient(
            max_retries=1,
            transport=httpx.MockTransport(lambda request: httpx.Response(next(statuses))),
        ) as http,
        GitHubClient(http_client=http) as github,
    ):
        assert github.get_file_content("a", "b", "missing.txt") is None
        with pytest.raises(HTTPClientError) as error:
            github.get_file_content("a", "b", "private.txt")
        assert error.value.status_code == 403
        with pytest.raises(HTTPClientError):
            RepositoryCollector(github, progress=progress).collect("a", "b")
    restored = manager.resume_run("github-errors")
    assert restored.error_count == 1
    assert restored.checkpoint_state == {}


def test_invalid_github_payload_is_counted_without_advancing_progress(tmp_path):
    manager = RunManager(tmp_path)
    progress = CollectionProgress(manager.start_run("invalid-payload"), manager)
    with (
        HTTPClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"created_at": "invalid"})
            )
        ) as http,
        GitHubClient(http_client=http) as github,
    ):
        with pytest.raises(ValueError):
            RepositoryCollector(github, progress=progress).collect("a", "b")
    restored = manager.resume_run("invalid-payload")
    assert restored.error_count == 1
    assert restored.collected_count == 0
    assert restored.checkpoint_state == {}
