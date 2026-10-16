"""Generate and validate a synthetic T-017 dataset offline, without credentials."""

import argparse
from datetime import UTC, datetime
from pathlib import Path

import httpx

from reposeer.collection.http.client import HTTPClient
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.pipelines.external_crawl import ExternalCrawlPipeline
from reposeer.schemas.external.candidate import ExternalCandidate
from reposeer.storage.paths import StoragePaths
from reposeer.storage.raw.jsonl import JsonlStorage


def run_sample(data_dir: Path):
    """Write a synthetic append-only capture and check strict replay."""
    html = (
        Path(__file__).resolve().parents[1] / "examples/external_crawl/article.html"
    ).read_bytes()

    def handler(request):
        if str(request.url) == "https://blog.example/synthetic-package":
            return httpx.Response(302, headers={"location": "/synthetic-package-v2"})
        if str(request.url) == "https://blog.example/synthetic-package-v2":
            return httpx.Response(
                200, content=html, headers={"content-type": "text/html; charset=utf-8"}
            )
        raise AssertionError("Unexpected offline request")

    candidate = ExternalCandidate(
        title="Synthetic Python package maintenance",
        url="https://blog.example/synthetic-package",
        source="hackernews",
        source_item_id="hn:900000042",
        query="synthetic package",
        query_id="synthetic-demo-query",
        matched_queries=["synthetic package"],
        discovered_at=datetime(2026, 10, 16, 5, tzinfo=UTC),
    )
    with HTTPClient(transport=httpx.MockTransport(handler)) as http:
        with ExternalCrawlPipeline(
            http_client=http,
            storage_paths=StoragePaths(data_dir),
            policy=URLPolicy(lambda host: ["93.184.216.34"]),
            clock=lambda: datetime(2026, 10, 16, 6, tzinfo=UTC),
        ) as crawler:
            result = crawler.crawl([candidate])
    if result.collection_result.status != "complete":
        raise AssertionError("Synthetic crawl failed")
    replayed = JsonlStorage().read_contract_records(result.output_path)
    if not any(
        r.run_id == result.run_id and r.payload["content_status"] == "full" for r in replayed
    ):
        raise AssertionError("Strict replay did not recover the sample")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, required=True, help="Separate synthetic output directory."
    )
    args = parser.parse_args()
    result = run_sample(args.data_dir)
    print(f"Synthetic offline dataset: {result.output_path}")
    print(f"Manifest: {result.manifest_path}")


if __name__ == "__main__":
    main()
