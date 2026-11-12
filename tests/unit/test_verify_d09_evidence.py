import base64
import gzip
import hashlib
import json
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from scripts import collect_d09_pilot as pilot
from scripts import verify_d09_evidence as verify
from scripts.d09_archive_pool import ArchivePool

ROW = {
    "package_id": "pypi:demo",
    "source_version": "1.0",
    "cutoff_at": "2024-09-30T23:59:59.999Z",
    "distribution_uploaded_at": "2024-09-26T08:00:00.000Z",
    "distribution_sha256": "a" * 64,
    "repo_url": "https://github.com/org/demo",
    "repo_id": "github:repository:123",
}


def event(identity=123):
    return (
        json.dumps(
            {
                "id": "456",
                "repo": {"id": identity, "name": "Org/Demo"},
                "created_at": "2024-09-27T15:31:00Z",
                "type": "ReleaseEvent",
            }
        ).encode()
        + b"\n"
    )


def test_verifies_original_gzip_and_reuses_only_hash_checked_evidence(tmp_path):
    line = event()
    body = gzip.compress(line)
    revision = {
        "name": "2024-09-27-15.json.gz",
        "generation": "42",
        "size": str(len(body)),
        "md5Hash": base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode(),
        "timeCreated": "2024-09-27T16:05:00.000Z",
    }
    local = tmp_path / revision["name"]
    local.write_bytes(body)

    source, lines = verify.archive_evidence(None, tmp_path, revision, {"org/demo"}, local)
    assert source["archive_sha256"] == hashlib.sha256(body).hexdigest()
    assert (
        verify.matching_original(ROW, source, lines)["record_sha256"]
        == hashlib.sha256(line.rstrip()).hexdigest()
    )
    assert verify.archive_evidence(None, tmp_path, revision, {"org/demo"}, None) == (source, lines)

    (tmp_path / source["path"]).write_bytes(b"unverified")
    local.write_bytes(bytes([body[0] ^ 1]) + body[1:])
    with pytest.raises(ValueError, match="archive_object_size_or_md5_mismatch"):
        verify.archive_evidence(None, tmp_path, revision, {"org/demo"}, local)


def test_rejects_archive_uploaded_after_cutoff_or_conflicting_repo_id():
    source = {"revision": {"timeCreated": "2024-10-01T00:05:00Z"}}
    assert verify.matching_original(ROW, source, [event()]) is None

    source["revision"]["timeCreated"] = "2024-09-27T16:05:00Z"
    with pytest.raises(ValueError, match="original_archive_repo_id_conflict"):
        verify.matching_original(ROW, source, [event(), event(321)])


def test_interrupted_archive_resumes_from_saved_bytes(tmp_path):
    body = gzip.compress(event())
    offset = len(body) // 2
    revision = {
        "name": "2024-09-27-15.json.gz",
        "generation": "42",
        "size": str(len(body)),
        "md5Hash": base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode(),
        "timeCreated": "2024-09-27T16:05:00.000Z",
    }
    ranges = []

    def request(req):
        ranges.append(req.headers.get("range"))
        headers = {"x-goog-generation": "42"}
        if len(ranges) == 1:
            return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body[:offset]))
        headers["Content-Range"] = f"bytes {offset}-{len(body) - 1}/{len(body)}"
        return httpx.Response(206, headers=headers, stream=httpx.ByteStream(body[offset:]))

    partial = tmp_path / f"{verify.archive_key(revision)}.part"
    with httpx.Client(transport=httpx.MockTransport(request)) as client:
        with pytest.raises(ValueError, match="archive_download_incomplete"):
            verify.archive_evidence(client, tmp_path, revision, {"org/demo"}, None)
        assert partial.read_bytes() == body[:offset]
        source, rows = verify.archive_evidence(client, tmp_path, revision, {"org/demo"}, None)
    assert ranges == [f"bytes=0-{len(body) - 1}", f"bytes={offset}-{len(body) - 1}"]
    assert source["archive_sha256"] == hashlib.sha256(body).hexdigest()
    assert verify.matching_original(ROW, source, rows) is not None
    assert not partial.exists()


def test_matches_exact_pypi_version_hash_and_upload_time(tmp_path, monkeypatch):
    payload = {
        "info": {"name": "Demo", "version": "1.0", "project_urls": {"Source": ROW["repo_url"]}},
        "urls": [
            {"digests": {"sha256": "a" * 64}, "upload_time_iso_8601": "2024-09-26T08:00:00.000123Z"}
        ],
    }
    monkeypatch.setattr(verify, "pypi_json", lambda *args: (payload, {}))
    _, uploaded = verify.pypi_evidence(None, tmp_path, ROW)
    assert verify.rounded_up_time(uploaded) == "2024-09-26T08:00:00.001Z"
    with pytest.raises(ValueError, match="pypi_artifact_digest_or_upload_time_mismatch"):
        verify.pypi_evidence(None, tmp_path, {**ROW, "distribution_sha256": "b" * 64})
    payload["info"]["project_urls"]["Source"] = "https://github.com/org/other"
    with pytest.raises(ValueError, match="pypi_explicit_source_mismatch"):
        verify.pypi_evidence(None, tmp_path, ROW)


def test_pypi_evidence_saves_and_reuses_response_under_windows_path_limit(tmp_path, monkeypatch):
    directory = tmp_path / ("s" * max(1, 140 - len(str(tmp_path.resolve())) - 1))
    payload = {
        "info": {"name": "Demo", "version": "1.0", "project_urls": {"Source": ROW["repo_url"]}},
        "urls": [{"digests": {"sha256": "a" * 64}, "upload_time_iso_8601": "2024-09-26T08:00:00Z"}],
    }
    body = json.dumps(payload).encode()
    requests = []

    def fetch(_http, url, _maximum):
        requests.append(url)
        return body, {
            "url": url,
            "content_sha256": hashlib.sha256(body).hexdigest(),
            "retrieved_at": "2026-11-11T00:00:00.000Z",
        }

    monkeypatch.setattr(pilot, "fetch_public", fetch)
    receipt, uploaded = verify.pypi_evidence(None, directory, ROW)
    retained = directory / receipt["path"]
    assert len(str(retained.resolve())) < 260
    assert retained.read_bytes() == body
    assert verify.pypi_evidence(None, directory, ROW) == (receipt, uploaded)
    assert len(requests) == 1


def test_archive_wave_prefers_coverage_per_byte_and_avoids_duplicate_work():
    hours = {"expensive": {1, 2, 3}, "cheap": {1, 2}, "overlaps": {1, 2}, "separate": {3}}
    costs = {"expensive": 100, "cheap": 40, "overlaps": 45, "separate": 30}
    assert verify.archive_wave(hours, {1, 2, 3}, costs, budget=2, workers=4) == [
        "cheap",
        "separate",
    ]
    costs["separate"] = 0
    assert verify.archive_wave(hours, {1, 2, 3}, costs, budget=0, workers=4) == ["separate"]


@pytest.mark.parametrize("workers", [2, 16])
def test_batch_checks_distinct_archives_concurrently_and_counts_the_budget(
    tmp_path, monkeypatch, workers
):
    candidates = [
        {
            **ROW,
            "package_id": f"pypi:package-{index}",
            "repo_url": f"https://github.com/org/package-{index}",
            "repo_id": f"github:repository:{1000 + index}",
        }
        for index in range(workers)
    ]
    hours = [f"2024-09-27T{index:02d}:00:00.000Z" for index in range(workers)]
    inventory = {
        hour: {
            "name": f"2024-09-27-{index}.json.gz",
            "generation": str(index + 1),
            "size": "100",
            "timeCreated": f"2024-09-27T{index + 1:02d}:05:00.000Z",
        }
        for index, hour in enumerate(hours)
    }
    monkeypatch.setattr(
        verify,
        "pypi_evidence",
        lambda *args: ({"artifact_uploaded_at": ROW["distribution_uploaded_at"]}, None),
    )
    monkeypatch.setattr(
        verify,
        "locate_hours",
        lambda *args: {
            (row["repo_id"], f"org/package-{index}"): {hours[index]}
            for index, row in enumerate(candidates)
        },
    )
    monkeypatch.setattr(verify, "verified_revision", lambda *args: {"content_sha256": "inventory"})
    barrier = Barrier(workers, timeout=10)

    def archive(*args):
        revision = args[2]
        index = int(revision["generation"]) - 1
        barrier.wait()
        payload = json.loads(event(1000 + index))
        payload["id"] = str(456 + index)
        payload["repo"]["name"] = f"org/package-{index}"
        payload["created_at"] = f"2024-09-27T{index:02d}:31:00Z"
        return {"revision": revision}, [json.dumps(payload).encode() + b"\n"]

    monkeypatch.setattr(verify, "archive_evidence", archive)
    args = SimpleNamespace(
        output_dir=tmp_path,
        inventory_dir=tmp_path,
        archive_dir=None,
        archive_workers=workers,
        max_downloads=workers,
    )
    rows, used = verify.verify_batch(args, candidates, inventory)
    assert used == workers
    assert [row["status"] for row in rows] == ["mapping_sources_matched"] * workers


@pytest.mark.parametrize("tamper", [False, True])
def test_archive_is_scanned_for_future_batches_and_reused_only_after_hash_check(
    tmp_path, monkeypatch, tamper
):
    other = {
        **ROW,
        "package_id": "pypi:other",
        "repo_url": "https://github.com/org/other",
        "repo_id": "github:repository:321",
    }
    body = gzip.compress(event() + event(321).replace(b"Org/Demo", b"Org/Other"))
    revision = {
        "name": "2024-09-27-15.json.gz",
        "generation": "42",
        "size": str(len(body)),
        "md5Hash": base64.b64encode(hashlib.md5(body, usedforsecurity=False).digest()).decode(),
        "timeCreated": "2024-09-27T16:05:00.000Z",
    }
    hour = "2024-09-27T15:00:00.000Z"
    inventory = {hour: revision}
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / revision["name"]).write_bytes(body)
    pool = ArchivePool(tmp_path, {"org/demo", "org/other"})
    monkeypatch.setattr(
        verify,
        "pypi_evidence",
        lambda *args: ({"artifact_uploaded_at": ROW["distribution_uploaded_at"]}, None),
    )
    monkeypatch.setattr(verify, "verified_revision", lambda *args: {"content_sha256": "listing"})
    locations = []

    def locate(http, directory, rows):
        locations.append(directory)
        return {
            (row["repo_id"], row["repo_url"].removeprefix("https://github.com/")): {hour}
            for row in rows
        }

    monkeypatch.setattr(verify, "locate_hours", locate)

    def options(index, archive_dir=None):
        return SimpleNamespace(
            output_dir=tmp_path / f"batch-{index}",
            inventory_dir=tmp_path,
            archive_dir=archive_dir,
            archive_pool=pool,
            archive_workers=1,
            max_downloads=0,
        )

    first, used = verify.verify_batch(options(1, raw), [ROW], inventory)
    assert used == 0 and first[0]["status"] == "mapping_sources_matched"
    shared = next(iter(pool.receipts.values()))
    assert shared["matched_names"] == ["org/demo", "org/other"]
    # A later invocation must discover the shared proof from disk as well.
    pool = ArchivePool(tmp_path, {"org/demo", "org/other"})
    if tamper:
        (pool.directory / shared["path"]).write_bytes(b"tampered")
    second, used = verify.verify_batch(options(2), [other], inventory)
    assert used == 0
    assert second[0]["status"] == (
        "download_budget_reached" if tamper else "mapping_sources_matched"
    )
    assert len(locations) == (2 if tamper else 1)
    if not tamper:
        source = second[0]["archive_source"]
        proof = (tmp_path / "batch-2" / source["path"]).read_bytes()
        assert hashlib.sha256(proof).hexdigest() == source["content_sha256"]
        assert source["archive_sha256"] == hashlib.sha256(body).hexdigest()
        wrong_id, _ = verify.verify_batch(
            options(3), [{**other, "repo_id": "github:repository:999"}], inventory
        )
        assert wrong_id[0]["status"] == "conflicting_original_repo_id"
