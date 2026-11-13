"""Persist T-017 current captures separately from T-016 discovery candidates."""

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.collectors.external.crawler.collector import (
    CandidateGroup,
    CandidateInput,
    ExternalCrawlerCollector,
    candidate_data,
    discovery_reference,
)
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.collectors.external.crawler.scope import RepositoryScopeEvidence
from reposeer.collectors.external.crawler.validation import validate_external_document_record
from reposeer.collectors.external.discovery.base import DiscoveryJob, load_discovery_state
from reposeer.contracts import (
    CollectionRequest,
    CollectionResult,
    CollectionSubject,
    RawRecord,
    validate_record,
)
from reposeer.contracts.identity import canonical_article_url
from reposeer.schemas.external.candidate import ExternalCandidate
from reposeer.storage.paths import StoragePaths, paths
from reposeer.storage.raw.jsonl import JsonlStorage


@dataclass(frozen=True)
class ExternalCrawlPipelineResult:
    collection_result: CollectionResult
    output_path: Path
    manifest_path: Path
    run_id: str


def _fingerprint(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _group_candidates(candidates: Iterable[CandidateInput]) -> tuple[list[CandidateGroup], int, dict[str, str]]:
    groups: list[CandidateGroup | None] = []
    parents: list[int] = []
    by_url: dict[str, int] = {}
    by_id: dict[str, int] = {}
    raw_fingerprints: dict[str, str] = {}
    count = 0

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for candidate in candidates:
        data = candidate_data(candidate)
        count += 1
        reference = discovery_reference(data) if data.get("discovery") else {}
        raw_id = data.get("candidate_raw_record_id")
        if raw_id:
            fingerprint = _fingerprint(data["discovery"])
            if raw_id in raw_fingerprints and raw_fingerprints[raw_id] != fingerprint:
                raise ValueError("conflicting_discovery_raw_id")
            raw_fingerprints[raw_id] = fingerprint
        url = canonical_article_url(data["url"])
        candidate_id = data.get("candidate_id")
        matches = {root(by_url[url])} if url in by_url else set()
        if isinstance(candidate_id, str) and candidate_id in by_id:
            matches.add(root(by_id[candidate_id]))
        if matches:
            index = min(matches)
            group = groups[index]
            for other in sorted(matches - {index}):
                group.references.update(groups[other].references)
                parents[other] = index
                groups[other] = None
        else:
            index = len(groups)
            group = CandidateGroup(candidate)
            groups.append(group)
            parents.append(index)
        by_url[url] = index
        if isinstance(candidate_id, str):
            by_id[candidate_id] = index
        if reference:
            key = raw_id or json.dumps(reference, sort_keys=True)
            group.references[key] = reference
    return [group for group in groups if group is not None], count, raw_fingerprints


def _select_candidate_groups(groups: list[CandidateGroup]) -> list[CandidateGroup]:
    """Rank identity metadata, then round-robin providers deterministically per rank."""
    ranks = {"exact_url_match": 0, "high_confidence_metadata": 1}
    buckets = defaultdict(lambda: defaultdict(deque))
    for group in sorted(groups, key=lambda g: canonical_article_url(candidate_data(g)["url"])):
        data = candidate_data(group)
        refs = list(group.references.values())
        rank = min((ranks.get(ref.get("subject_match_status"), 2) for ref in refs), default=2)
        provider = min((ref.get("discovery_provider", "web") for ref in refs), default=data.get("discovery_provider", "web"))
        buckets[rank][provider].append(group)
    result = []
    for rank in sorted(buckets):
        providers = buckets[rank]
        while any(providers.values()):
            for provider in sorted(providers):
                if providers[provider]:
                    result.append(providers[provider].popleft())
    return result


def _apply_projection_states(groups: list[CandidateGroup], state_paths: Sequence[Path],
                             input_fingerprints: dict[str, str]) -> None:
    """Only explicitly supplied, matching T-016 snapshots may establish lineage completeness."""
    targets = {}
    for group in groups:
        for reference in group.references.values():
            key = (reference.get("discovery_run_id"), reference.get("candidate_id"))
            if all(key):
                targets[key] = group
    for path in state_paths:
        state = load_discovery_state(Path(path))
        planned_hash = state["scope"].get("planned_query_set_hash")
        if not isinstance(planned_hash, str) or len(planned_hash) != 64 or any(
            character not in "0123456789abcdef" for character in planned_hash
        ):
            raise ValueError("projection_invalid_query_scope")
        jobs = {job.job_id: job for job in map(DiscoveryJob.model_validate, state["jobs"])}
        if len(jobs) != len(state["jobs"]):
            raise ValueError("duplicate_projection_job")
        records = {}
        for raw_data in state["records"]:
            raw = validate_record(raw_data)
            if not isinstance(raw, RawRecord):
                raise ValueError("projection_requires_discovery_raw_records")
            candidate_data(raw)
            fingerprint = _fingerprint(raw.model_dump(mode="json"))
            if raw.raw_record_id in input_fingerprints and input_fingerprints[raw.raw_record_id] != fingerprint:
                raise ValueError("projection_observation_integrity_mismatch")
            if raw.raw_record_id in records and records[raw.raw_record_id] != raw:
                raise ValueError("conflicting_projection_raw_id")
            if raw.payload.get("query_set_hash") != planned_hash:
                raise ValueError("projection_query_scope_mismatch")
            records[raw.raw_record_id] = raw
            input_fingerprints[raw.raw_record_id] = fingerprint
        if len({raw.run_id for raw in records.values()}) > 1:
            raise ValueError("projection_contains_multiple_discovery_runs")
        referenced_raw_ids: set[str] = set()
        projected_ids: set[str] = set()
        for projection_data in state["candidate_projections"]:
            projection = ExternalCandidate.model_validate(projection_data)
            if projection.candidate_id in projected_ids:
                raise ValueError("duplicate_projection_candidate")
            projected_ids.add(projection.candidate_id)
            referenced_raw_ids.update(projection.raw_record_ids)
            if not projection.raw_record_ids or len(set(projection.raw_record_ids)) != len(projection.raw_record_ids):
                raise ValueError("projection_requires_distinct_raw_references")
            for raw_id in projection.raw_record_ids:
                if raw_id not in records or records[raw_id].payload.get("candidate_id") != projection.candidate_id:
                    raise ValueError("projection_candidate_identity_mismatch")
            associations = {}
            for association in projection.query_associations:
                job = jobs.get(association.get("job_id"))
                if job is None or projection.candidate_id not in job.candidate_ids:
                    raise ValueError("projection_query_job_mismatch")
                expected = {"query_id": job.query_id, "job_id": job.job_id, "provider": job.provider,
                            "intent": job.query_intent, "context_level": job.context_level, "query": job.query}
                if job.tech_slug is not None:
                    expected["tech_slug"] = job.tech_slug
                if association != expected:
                    raise ValueError("projection_query_association_mismatch")
                associations[job.job_id] = association
            if set(associations) != {job.job_id for job in jobs.values() if projection.candidate_id in job.candidate_ids}:
                raise ValueError("projection_missing_query_associations")
            for raw_id in projection.raw_record_ids:
                raw = records[raw_id]
                group = targets.get((raw.run_id, projection.candidate_id))
                if group is not None:
                    reference = discovery_reference(candidate_data(raw))
                    previous = group.references.get(raw_id, {})
                    union = {json.dumps(entry, sort_keys=True): entry for entry in [
                        *previous.get("query_associations", []), *associations.values(),
                    ]}
                    projection_paths = sorted({*previous.get("projection_state_paths", []), str(Path(path).resolve())})
                    reference.update(query_associations=[union[key] for key in sorted(union)],
                                     projection_verified=True, projection_state_paths=projection_paths)
                    group.references[raw_id] = reference
        if referenced_raw_ids != records.keys():
            raise ValueError("projection_missing_raw_references")


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
        candidates: Iterable[CandidateInput],
        *,
        max_candidates: int = 100,
        cursor: str | None = None,
        discovery_state_paths: Sequence[Path] = (),
    ) -> ExternalCrawlPipelineResult:
        if type(max_candidates) is not int or not 1 <= max_candidates <= 1000:
            raise ValueError("max_candidates_must_be_between_1_and_1000")
        if cursor is not None and (not isinstance(cursor, str) or not cursor.isdecimal()):
            raise ValueError("invalid_candidate_cursor")
        groups, input_count, fingerprints = _group_candidates(candidates)
        if not groups:
            raise ValueError("provide_at_least_one_candidate")
        offset = int(cursor) if cursor is not None else 0
        if offset >= len(groups):
            raise ValueError("no_remaining_candidates")
        _apply_projection_states(groups, discovery_state_paths, fingerprints)
        if len(groups) > max_candidates:
            groups = _select_candidate_groups(groups)
        batch = groups[offset:offset + max_candidates]
        data = [candidate_data(group) for group in batch]
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
        result = self.collector.collect(request, groups)
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
                "input_candidate_count": input_count,
                "unique_candidate_count": len(groups),
                "deduplicated_candidate_count": input_count - len(groups),
                "attempted_candidate_count": len(batch),
                "candidate_groups": [{"url": value["url"], "candidate_id": value.get("candidate_id"),
                                      "candidate_raw_record_id": value.get("candidate_raw_record_id"),
                                      "discovery_references": value["discovery_references"],
                                      "discovery_lineage_complete": value["discovery_lineage_complete"],
                                      "errors": group.errors}
                                     for group, value in zip(batch, data, strict=True)],
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
        self, candidates: Iterable[CandidateInput], **kwargs: Any
    ) -> ExternalCrawlPipelineResult:
        return self.crawl(candidates, **kwargs)
