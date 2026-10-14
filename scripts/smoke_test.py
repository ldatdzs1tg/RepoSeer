"""T-010 vertical smoke: canonical GitHub observation -> technology -> HN candidate -> raw replay."""

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx

from reposeer.clients.external.hackernews import HackerNewsClient
from reposeer.clients.github import GitHubClient
from reposeer.collection.http.client import HTTPClient
from reposeer.contracts import RawRecord, validate_record
from reposeer.contracts.identity import canonical_article_url, format_utc_timestamp
from reposeer.exceptions import CollectionError
from reposeer.services.repository_service import RepositoryService, parse_repository_target
from reposeer.storage.paths import StoragePaths
from reposeer.storage.query.duckdb import DuckDBQueryEngine
from reposeer.storage.raw.jsonl import JsonlStorage
from reposeer.technology import TechnologyDetector


def offline_transport(target: str) -> httpx.MockTransport:
    """Synthetic provider responses only; target identity still crosses the real contract boundary."""
    payload = json.loads((Path(__file__).resolve().parents[1] /
                          "examples/contracts/github_raw_record.json").read_text(encoding="utf-8"))["payload"]
    payload.update(full_name=target, id=900000001, topics=["python"], language="Python")

    def handler(request):
        if request.url.host == "api.github.com" and request.url.path == f"/repos/{target}":
            return httpx.Response(200, json=payload)
        if request.url.host == "hn.algolia.com" and request.url.path == "/api/v1/search":
            return httpx.Response(200, json={"hits": [{
                "objectID": "42", "title": "Synthetic discovery candidate",
                "url": "https://example.org/synthetic-story", "points": None,
            }]})
        raise AssertionError(f"Unexpected offline request: {request.url.host}{request.url.path}")

    return httpx.MockTransport(handler)


def run_smoke_test(target: str = "octocat/Hello-World", *, offline: bool = True,
                   data_dir: Path, transport: httpx.BaseTransport | None = None,
                   clock: Callable[[], datetime] | None = None) -> dict:
    target = parse_repository_target(target)
    clock = clock or ((lambda: datetime(2024, 2, 29, 12, tzinfo=UTC)) if offline
                      else (lambda: datetime.now(UTC)))
    if offline and transport is None:
        transport = offline_transport(target)
    paths = StoragePaths(data_dir)
    storage = JsonlStorage()
    with HTTPClient(transport=transport, max_retries=2, timeout=20) as http:
        with GitHubClient(http_client=http, token="synthetic-token" if offline else None) as github:
            service = RepositoryService(github, storage_paths=paths)
            service.collector.clock = clock
            owner, repository = target.split("/")
            normalized = service.collect_and_store(owner, repository)
            github_raw = storage.read_contract_records(paths.raw_path("github", "repositories"))
            raw = next(record for record in github_raw
                       if record.raw_record_id == normalized.provenance.raw_record_ids[0])
            detection = TechnologyDetector().detect(raw)
            term = next((term for term in detection.search_terms if term.platform == "hackernews"),
                        detection.search_terms[0] if detection.search_terms else None)
            if term is None:
                raise CollectionError("No bounded technology or verified subject query")
            with HackerNewsClient(http_client=http, clock=clock) as hn:
                candidates = hn.search(term.query, limit=1)
            external_path = paths.raw_path("external", "discovery")
            for candidate in candidates[:1]:
                external = RawRecord(
                    schema_version="0.1.0", record_type="raw_record",
                    raw_record_id="raw:" + str(uuid4()), run_id=raw.run_id,
                    source="web_article", source_record_type="article", source_record_id=None,
                    repo_id=None, package_id=None, source_url=canonical_article_url(candidate.url),
                    retrieved_at=format_utc_timestamp(clock()), event_at=None, available_at=None,
                    http_status=None, raw_body_sha256=None,
                    payload={**candidate.model_dump(mode="json"), "query_intent": term.intent,
                             "record_stage": "discovery_candidate", "discovery_status": "unverified",
                             "content_status": "not_fetched", "discovery_provider": "hackernews"},
                )
                storage.append_contract_record(external_path, validate_record(external))
            external_raw = storage.read_contract_records(external_path)
    engine = DuckDBQueryEngine()
    try:
        engine.register_parquet("repositories", paths.processed_path("repositories"))
        processed_count = engine.execute_sql("SELECT count(*) FROM repositories")[0][0]
    finally:
        engine.close()
    summary = {"status": "complete" if candidates else "external_unavailable",
               "github_raw_records": len(github_raw), "external_raw_records": len(external_raw),
               "processed_repositories": processed_count, "query": term.query,
               "mode": "offline" if offline else "live"}
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", default="octocat/Hello-World")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="Deterministic mock transport (default)")
    mode.add_argument("--live", action="store_true", help="Opt in to live GitHub and HN requests")
    args = parser.parse_args()
    try:
        with TemporaryDirectory(prefix="reposeer-smoke-") as directory:
            result = run_smoke_test(args.target, offline=not args.live, data_dir=Path(directory))
        return 0 if result["status"] == "complete" else 1
    except (CollectionError, ValueError, OSError) as error:
        print(f"Smoke failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
