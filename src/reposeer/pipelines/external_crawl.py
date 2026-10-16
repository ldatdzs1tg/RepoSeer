"""Persist T-017 current captures separately from T-016 discovery candidates."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.collectors.external.crawler.collector import (
    CandidateInput,
    ExternalCrawlerCollector,
    candidate_data,
)
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.collectors.external.crawler.scope import RepositoryScopeEvidence
from reposeer.collectors.external.crawler.validation import validate_external_document_record
from reposeer.contracts import CollectionRequest, CollectionResult, CollectionSubject
from reposeer.storage.paths import StoragePaths, paths
from reposeer.storage.raw.jsonl import JsonlStorage


@dataclass(frozen=True)
class ExternalCrawlPipelineResult:
    collection_result: CollectionResult
    output_path: Path
    manifest_path: Path
    run_id: str


class ExternalCrawlPipeline:
    """Own only self-created sessions; save raw before publishing a run manifest."""

    def __init__(
        self,
        *,
        http_client: HTTPClient | None = None,
        storage_paths: StoragePaths | None = None,
        storage: JsonlStorage | None = None,
        policy: URLPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        max_redirects: int = 5,
        max_response_bytes: int = 5_000_000,
        repository_evidence: Sequence[RepositoryScopeEvidence] = (),
    ):
        self._owns_http = http_client is None
        self.http = http_client if http_client is not None else HTTPClient()
        self.paths = storage_paths or paths
        self.storage = storage or JsonlStorage()
        self.collector = ExternalCrawlerCollector(
            self.http,
            policy=policy,
            clock=clock,
            max_redirects=max_redirects,
            max_response_bytes=max_response_bytes,
            repository_evidence=repository_evidence,
        )

    def __enter__(self) -> "ExternalCrawlPipeline":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def crawl(
        self,
        candidates: Sequence[CandidateInput],
        *,
        max_candidates: int = 100,
        cursor: str | None = None,
    ) -> ExternalCrawlPipelineResult:
        if not candidates or len(candidates) > 1000:
            raise ValueError("provide_between_1_and_1000_candidates")
        if type(max_candidates) is not int or not 1 <= max_candidates <= 1000:
            raise ValueError("max_candidates_must_be_between_1_and_1000")
        # Validate all inputs before any network or persistence side effects.
        data = [candidate_data(value) for value in candidates]
        run_id = str(uuid4())
        request = CollectionRequest(
            schema_version="0.1.0",
            record_type="collection_request",
            request_id=str(uuid4()),
            run_id=run_id,
            collector_name="external_crawler",
            source="web_article",
            subjects=[
                CollectionSubject(
                    repo_id=None,
                    repo_full_name=None,
                    package_id=None,
                    url=value["url"],
                )
                for value in data
            ],
            record_kinds=["article"],
            time_range=None,
            cutoff_at=None,
            cursor=cursor,
            max_pages=max_candidates,
            max_records=max_candidates,
        )
        result = self.collector.collect(request, candidates)
        for record in result.records:
            validate_external_document_record(record)
        output_path = self.paths.raw_path("external", "documents")
        self.storage.append_contract_records_durable(output_path, result.records)
        manifest_path = (
            self.paths.metadata_dir / "collection_runs" / f"crawl_manifest_{run_id}.json"
        )
        write_json_atomic(
            manifest_path,
            {
                **result.model_dump(mode="json", exclude={"records"}),
                "output_path": str(output_path),
                "input_candidate_count": len(candidates),
                "raw_record_ids": [r.raw_record_id for r in result.records],
                "documents_extracted": sum(
                    r.payload["content_status"] == "full" for r in result.records
                ),
                "coverage_scope": "requested_candidate_batch_only",
                "historical_version_status": "observation_only",
            },
        )
        return ExternalCrawlPipelineResult(result, output_path, manifest_path, run_id)

    def run(
        self, candidates: Sequence[CandidateInput], **kwargs: Any
    ) -> ExternalCrawlPipelineResult:
        return self.crawl(candidates, **kwargs)
