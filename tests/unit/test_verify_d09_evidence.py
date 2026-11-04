import base64
import gzip
import hashlib
import json

import pytest
from scripts import verify_d09_evidence as verify

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
