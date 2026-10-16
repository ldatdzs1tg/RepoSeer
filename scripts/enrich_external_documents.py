"""Refetch saved external resources with verified current repository scope.

Requires --live. Original raw lines are preserved; new observations append, and
a validated latest-per-URL view is written under reports for inspection.
"""

import argparse
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from reposeer.clients.github import GitHubClient
from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.collectors.external.crawler.scope import RepositoryScopeEvidence
from reposeer.contracts import RawRecord, validate_record
from reposeer.contracts.identity import format_utc_timestamp
from reposeer.pipelines.external_crawl import ExternalCrawlPipeline
from reposeer.services.repository_service import RepositoryService, parse_repository_target
from reposeer.storage.paths import StoragePaths
from reposeer.storage.raw.jsonl import JsonlStorage


def enrich(data_dir: Path):
    paths, storage = StoragePaths(data_dir), JsonlStorage()
    document_path = paths.raw_path("external", "documents")
    original_bytes = document_path.read_bytes()
    originals = storage.read_contract_records(document_path)
    latest = {r.source_url: r for r in originals}
    if not 1 <= len(latest) <= 1000:
        raise ValueError("expected_between_1_and_1000_saved_resources")
    candidates = [
        validate_record(r.payload["discovery"])
        if r.payload.get("candidate_raw_record_id")
        else r.source_url
        for r in latest.values()
    ]
    packages = sorted({r.package_id for r in latest.values() if r.package_id})
    scopes = []
    with HTTPClient(timeout=20, max_retries=2) as http:
        github = GitHubClient(http_client=http)
        service = RepositoryService(client=github, storage_paths=paths)
        for package_id in packages:
            name = package_id.removeprefix("pypi:")
            source_url = f"https://pypi.org/pypi/{name}/json"
            response = http.get(source_url, use_cache=False)
            observed = format_utc_timestamp(datetime.now(UTC))
            payload = response.json()
            links = payload["info"].get("project_urls") or {}
            slugs = {
                parse_repository_target(url)
                for label, url in links.items()
                if label.casefold() in {"source", "repository", "code"}
            }
            if len(slugs) != 1:
                raise ValueError("ambiguous_or_missing_pypi_source_repository")
            slug = next(iter(slugs))
            owner, repo = slug.split("/")
            repository = service.collect_and_store(owner, repo)
            raw_id = repository.provenance.raw_record_ids[0]
            github_raw = next(
                r
                for r in storage.read_contract_records(paths.raw_path("github", "repositories"))
                if r.raw_record_id == raw_id
            )
            pypi_raw = RawRecord(
                schema_version="0.1.0",
                record_type="raw_record",
                raw_record_id=f"raw:{uuid4()}",
                run_id=str(uuid4()),
                source="pypi_json",
                source_record_type="dependency_metadata",
                source_record_id=name,
                repo_id=None,
                package_id=package_id,
                source_url=source_url,
                retrieved_at=observed,
                event_at=None,
                available_at=observed,
                http_status=response.status_code,
                raw_body_sha256=hashlib.sha256(response.content).hexdigest(),
                payload=payload,
            )
            scope = RepositoryScopeEvidence(pypi_raw, github_raw)
            resolved = scope.resolve()
            storage.append_contract_records_durable(paths.raw_path("pypi", "packages"), [pypi_raw])
            scopes.append(scope)
            print(f"Verified {package_id} -> {resolved['repo_id']} ({slug})", flush=True)
        with ExternalCrawlPipeline(
            http_client=http, storage_paths=paths, repository_evidence=scopes
        ) as crawler:
            result = crawler.crawl(candidates, max_candidates=len(candidates))
    assert document_path.read_bytes().startswith(original_bytes), (
        "Original raw observations changed"
    )
    latest = {r.source_url: r for r in storage.read_contract_records(document_path)}
    view = paths.reports_dir / "t017_latest_documents.jsonl"
    storage.write(view, list(latest.values()))
    storage.read_contract_records(view)
    old_latest = {r.source_url: r for r in originals}
    changes = []
    for url, current in latest.items():
        before = old_latest[url]
        filled = [
            key
            for key in ["repo_id", "package_id", "available_at", "source_record_id"]
            if getattr(before, key) is None and getattr(current, key) is not None
        ]
        filled += [
            f"payload.{key}"
            for key in [
                "author",
                "published_at",
                "published_date",
                "updated_at",
                "content_available_at",
            ]
            if before.payload.get(key) is None and current.payload.get(key) is not None
        ]
        changes.append(
            {
                "url": url,
                "previous_raw_record_id": before.raw_record_id,
                "current_raw_record_id": current.raw_record_id,
                "filled_fields": filled,
            }
        )
    report = {
        "run_id": result.run_id,
        "status": result.collection_result.status,
        "raw_dataset": str(document_path),
        "latest_view": str(view),
        "manifest": str(result.manifest_path),
        "original_observations_preserved": True,
        "original_observation_count": len(originals),
        "unique_resources": len(latest),
        "changes": changes,
        "remaining_nulls": {
            key: sum(r.payload.get(key) is None for r in latest.values())
            for key in [
                "author",
                "published_at",
                "published_date",
                "updated_at",
                "content_available_at",
            ]
        },
        "repo_id_populated": sum(r.repo_id is not None for r in latest.values()),
        "available_at_populated": sum(r.available_at is not None for r in latest.values()),
    }
    write_json_atomic(paths.reports_dir / "t017_enrichment.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--live", action="store_true", help="Allow actual upstream requests.")
    args = parser.parse_args()
    if not args.live:
        parser.error("This command requires --live.")
    report = enrich(args.data_dir)
    print(f"Status: {report['status']}; latest view: {report['latest_view']}")
    if report["status"] != "complete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
