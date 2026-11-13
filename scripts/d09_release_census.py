"""Restore removed first-release files only from checksum-pinned publisher artifacts."""

import json
import re
from pathlib import Path

from scripts.collect_d09_batch import archive_timestamp, query_rows, sql_list
from scripts.collect_d09_pilot import fetch_public, provider_time, stored_source_matches
from scripts.d09_artifact_metadata import core_metadata
from scripts.run_d09_drive import digest
from tools.coordination._common import atomic_bytes

from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.identity import format_utc_timestamp, package_id
from reposeer.exceptions import HTTPClientError


def recover_artifacts(http, directory: Path, subject: str, history, project, budget: int):
    """A missing JSON entry is not a zero release. Retain and verify original bytes."""
    path = directory / "artifact-proofs.json"
    if budget == 0 and not path.is_file():
        return set(), {}, 0
    proofs = json.loads(path.read_bytes()) if path.exists() else {}
    wanted = {}
    for row in history:
        files = project["releases"].get(row["version"], [])
        if not any(
            item["digests"]["sha256"] == row["sha256"]
            and format_utc_timestamp(provider_time(item["upload_time_iso_8601"]))
            == row["uploaded_at"]
            for item in files
        ):
            wanted[row["sha256"]] = row
    pending = [sha for sha in sorted(wanted) if sha not in proofs]
    selected = pending[:budget]
    if selected:
        query = f"""SELECT name, version, toString(upload_time, 'UTC') AS uploaded_at,
          filename, path, sha256_digest AS sha256
FROM pypi.projects
WHERE name IN ({sql_list({row["name"] for row in wanted.values()})})
  AND sha256_digest IN ({sql_list(selected)})
ORDER BY name, version, uploaded_at, sha256
FORMAT JSONEachRow"""
        key = "restorable-artifacts-" + digest(query.encode())[:16]
        rows = query_rows(http, directory, key, query, post=True)
        receipt_path = directory / "sources" / (key + ".json")
        receipt = json.loads(receipt_path.read_bytes())
        for row in rows:
            sha = row["sha256"]
            prior = wanted.get(sha)
            if (
                prior is None
                or package_id(row["name"]) != subject
                or row["version"] != prior["version"]
                or archive_timestamp(row["uploaded_at"]) != prior["uploaded_at"]
                or not re.fullmatch(r"[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{60}/[\w.\-]+", row["path"])
            ):
                raise ValueError("restorable artifact differs from the frozen release history")
            url = "https://files.pythonhosted.org/packages/" + row["path"]
            try:
                body, source = fetch_public(http, url, 32 * 1024 * 1024)
            except HTTPClientError as error:
                if error.status_code != 404:
                    raise
                proofs[sha] = {"status": "unavailable", "url": url}
                write_json_atomic(path, proofs)
                continue
            if digest(body) != sha:
                raise ValueError("restored artifact checksum differs from historical metadata")
            core_metadata(body, subject, row["version"], require_repository=False)
            source["path"] = "artifacts/" + sha + ".distribution"
            atomic_bytes(directory / source["path"], body, immutable=False)
            proofs[sha] = {
                "status": "verified",
                "artifact": source,
                "historical_record": row,
                "historical_receipt": receipt,
            }
            write_json_atomic(path, proofs)
    verified, materials = set(), {}
    for sha, proof in proofs.items():
        if sha not in wanted or proof["status"] != "verified":
            continue
        original, src, receipt = (
            proof["historical_record"],
            proof["artifact"],
            proof["historical_receipt"],
        )
        if (
            original["sha256"] != sha
            or original["version"] != wanted[sha]["version"]
            or package_id(original["name"]) != subject
            or archive_timestamp(original["uploaded_at"]) != wanted[sha]["uploaded_at"]
            or src["content_sha256"] != sha
            or not stored_source_matches(directory, src)
            or not stored_source_matches(directory, receipt)
            or original
            not in [
                json.loads(line) for line in (directory / receipt["path"]).read_bytes().splitlines()
            ]
        ):
            raise ValueError("a restored release proof is missing, corrupt or out of scope")
        core_metadata(
            (directory / src["path"]).read_bytes(),
            subject,
            original["version"],
            require_repository=False,
        )
        verified.add(sha)
        for retained in (src, receipt):
            materials[str((directory / retained["path"]).resolve())] = retained["content_sha256"]
    if path.exists():
        materials[str(path.resolve())] = digest(path.read_bytes())
    return verified, materials, len(selected)
