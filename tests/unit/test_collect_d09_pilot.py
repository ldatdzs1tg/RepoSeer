import gzip
import hashlib
import json
from datetime import datetime

import httpx
import pytest
from scripts.collect_d09_pilot import collect_archive_hour, collect_package, matching_events

from reposeer.collection.http.client import HTTPClient

CUTOFF = datetime.fromisoformat("2024-12-31T23:59:59.999Z")
HORIZON_END = datetime.fromisoformat("2025-12-31T23:59:59.999Z")
LOOKBACK_START = datetime.fromisoformat("2023-12-31T23:59:59.999Z")


def artifact(name: str, uploaded_at: str, sha: str = "a" * 64) -> dict:
    return {
        "url": f"https://files.pythonhosted.org/packages/{name}",
        "upload_time_iso_8601": uploaded_at,
        "digests": {"sha256": sha},
    }


@pytest.mark.parametrize("version_sha", ["a" * 64, "b" * 64])
def test_mapping_reads_only_pre_cutoff_version_and_matches_the_artifact(tmp_path, version_sha):
    old_artifact = artifact("example-1.0.whl", "2024-12-31T23:59:59.998Z")
    new_artifact = artifact("example-2.0.whl", "2024-12-31T23:59:59.999500Z")
    later_artifact = artifact("example-3.0.whl", "2026-01-01T00:00:00Z")
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/pypi/example/json":
            return httpx.Response(
                200,
                json={
                    "info": {"project_urls": {"Source": "https://github.com/owner/new-repo"}},
                    "releases": {
                        "1.0": [old_artifact],
                        "2.0": [new_artifact],
                        "3.0": [later_artifact],
                    },
                },
            )
        assert request.url.path == "/pypi/example/1.0/json"
        return httpx.Response(
            200,
            json={
                "info": {
                    "name": "Example",
                    "version": "1.0",
                    "project_urls": {"Source": "https://github.com/owner/original"},
                },
                "urls": [{**old_artifact, "digests": {"sha256": version_sha}}],
            },
        )

    with HTTPClient(transport=httpx.MockTransport(respond), max_retries=1) as http:
        row = collect_package(http, "pypi:example", CUTOFF, HORIZON_END, tmp_path)

    assert calls == ["/pypi/example/json", "/pypi/example/1.0/json"]
    assert [(release["version"], release["phase"]) for release in row["releases"]] == [
        ("1.0", "pre_cutoff"),
        ("2.0", "outcome_horizon"),
    ]
    for source in row["pypi_sources"].values():
        assert (
            hashlib.sha256((tmp_path / source["path"]).read_bytes()).hexdigest()
            == source["content_sha256"]
        )
    assert row["mapping_history_verified"] is False
    assert row["label_status"] == "not_evaluated"
    if version_sha == "a" * 64:
        assert row["status"] == "repo_candidate"
        assert row["repo_candidate_url"] == "https://github.com/owner/original"
    else:
        assert row["status"] == "pypi_evidence_invalid"
        assert row["reason"] == "pypi_version_artifact_mismatch"
        assert row["repo_candidate_url"] is None


def test_archive_separates_horizon_and_keeps_availability_unknown():
    def event(name, occurred_at):
        return {"repo": {"id": 123, "name": name}, "created_at": occurred_at, "type": "PushEvent"}

    records = [
        event("owner/original", "2023-12-31T23:59:59.999Z"),
        event("OWNER/original", "2024-12-31T23:59:59.999Z"),
        event("owner/original", "2024-12-31T23:59:59.999500Z"),
        event("owner/other", "2025-01-01T00:00:00Z"),
        event("owner/original", "2026-01-01T00:00:00Z"),
    ]
    body = gzip.compress("\n".join(map(json.dumps, records)).encode())
    rows = matching_events(body, {"owner/original"}, LOOKBACK_START, CUTOFF, HORIZON_END)
    assert [row["phase"] for row in rows] == ["pre_cutoff", "outcome_horizon"]
    assert {row["repo_id"] for row in rows} == {"github:repository:123"}
    assert all(row["available_at"] is None for row in rows)


def test_archive_can_replay_a_local_hour_without_using_http(tmp_path):
    hour = datetime.fromisoformat("2024-12-25T04:00:00Z")
    event = {
        "repo": {"id": 123, "name": "owner/original"},
        "created_at": "2024-12-25T04:10:00Z",
        "type": "PushEvent",
    }
    archive = tmp_path / "2024-12-25-4.json.gz"
    archive.write_bytes(gzip.compress(json.dumps(event).encode()))
    packages = [
        {
            "repo_candidate_url": "https://github.com/owner/original",
            "archive_repo_ids_dated_by_cutoff": [],
            "archive_events_dated_by_cutoff": 0,
            "archive_events_in_horizon": 0,
        }
    ]

    def unexpected_request(request):
        pytest.fail(f"Local replay attempted HTTP: {request.url}")

    with HTTPClient(transport=httpx.MockTransport(unexpected_request), max_retries=1) as http:
        source = collect_archive_hour(
            http,
            hour,
            packages,
            LOOKBACK_START,
            CUTOFF,
            HORIZON_END,
            tmp_path,
            archive_dir=tmp_path,
        )

    assert source["status"] == "observed"
    assert source["url"] is None
    assert source["content_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert packages[0]["archive_repo_ids_dated_by_cutoff"] == ["github:repository:123"]
    assert packages[0]["archive_events_dated_by_cutoff"] == 1
