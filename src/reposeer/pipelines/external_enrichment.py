"""External-only orchestration over immutable, previously verified identity observations."""

import json
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.collection.provider_preflight import (
    EXTERNAL_SOURCES,
    TERMINAL_PROVIDER_STATES,
    ProviderState,
)
from reposeer.collection.run_manager import RunManager
from reposeer.collectors.external.crawler.scope import RepositoryScopeEvidence
from reposeer.contracts import RawRecord, validate_record
from reposeer.contracts.identity import canonical_article_url
from reposeer.pipelines.external import ExternalPipeline
from reposeer.pipelines.external_crawl import ExternalCrawlPipeline
from reposeer.pipelines.normalization import NormalizationPipeline
from reposeer.pipelines.processed import ProcessedDatasetPipeline
from reposeer.processing.relevance import VerifiedTargetContext
from reposeer.storage.paths import StoragePaths
from reposeer.storage.raw.jsonl import JsonlStorage


def source_scopes(source: Path, manifest: dict) -> list[RepositoryScopeEvidence]:
    """Read only the exact PyPI/GitHub observations referenced by the verified manifest."""
    wanted = {raw_id for target in manifest["targets"] for raw_id in target["mapping_evidence_raw_ids"]}
    records = {}
    for name in ("raw/pypi/packages.jsonl", "raw/github/repositories.jsonl"):
        with (source / name).open("rb") as stream:
            while line := stream.readline(16 * 1024**2 + 1):
                if len(line) > 16 * 1024**2:
                    raise ValueError("raw_line_size_limit")
                raw = validate_record(line)
                if not isinstance(raw, RawRecord):
                    raise ValueError("noncanonical_raw_evidence")
                if raw.raw_record_id in wanted:
                    if raw.raw_record_id in records and records[raw.raw_record_id] != raw:
                        raise ValueError("raw_id_conflict")
                    records[raw.raw_record_id] = raw
    scopes = []
    for target in manifest["targets"]:
        evidence = [records[key] for key in target["mapping_evidence_raw_ids"]]
        packages = [raw for raw in evidence if raw.source == "pypi_json" and raw.package_id == target["package_id"]]
        if len(packages) != 1:
            raise ValueError("pypi_mapping_evidence_missing")
        scope = RepositoryScopeEvidence(packages[0], records[target["repository_raw_record_id"]])
        resolved = scope.resolve()
        if resolved["repo_id"] != target["repo_id"]:
            raise ValueError("source_identity_mismatch")
        scopes.append(scope)
    if not scopes or len({scope.github.repo_id for scope in scopes}) != 1:
        raise ValueError("source_identity_mismatch")
    return scopes


def discovery_status(jobs: list[dict]) -> str:
    """Budget completion is terminal; incomplete pagination is still reported in jobs."""
    reasons = {job.get("missing_reason") for job in jobs}
    errors = [error for job in jobs for error in job.get("errors", [])]
    if reasons & {"quota_exhausted", "rate_limited", "quota_deferred"}:
        return ProviderState.QUOTA
    if reasons & {"invalid_credentials", "missing_configuration", "provider_plan_restricted", "preflight_deferred"}:
        return ProviderState.BLOCKED
    if any(error["code"] in {"parse_error", "validation_error"} for error in errors):
        return ProviderState.INSPECT
    if errors or not jobs or any(job["status"] in {"failed", "unavailable"} for job in jobs):
        return ProviderState.RETRY
    if reasons & {"max_pages", "max_records", "provider_record_budget", "total_request_budget"}:
        return ProviderState.BUDGET
    if all(job["status"] == "complete" for job in jobs):
        return ProviderState.COMPLETE if any(job["candidate_ids"] for job in jobs) else ProviderState.EMPTY
    return ProviderState.RETRY


def provider_deferrals(jobs: list[dict]) -> dict:
    """Share actual provider failures across targets without treating budgets as outages."""
    result = {}
    for name in {job["provider"] for job in jobs}:
        own = [job for job in jobs if job["provider"] == name and job.get("errors")]
        status = discovery_status(own)
        if own and status not in TERMINAL_PROVIDER_STATES:
            result[name] = status
    return result


class ExternalEnrichmentPipeline:
    """One package/provider workspace. Coordination owns receipts and global URL selection."""

    def __init__(self, storage_paths: StoragePaths, *, http_client: HTTPClient | None = None,
                 discovery_factory=ExternalPipeline, crawl_factory=ExternalCrawlPipeline,
                 url_policy=None):
        self.paths, self.http = storage_paths, http_client
        self.discovery_factory, self.crawl_factory = discovery_factory, crawl_factory
        self.url_policy = url_policy
        self.storage = JsonlStorage()

    def prepare(self, scope: RepositoryScopeEvidence, options: dict) -> dict:
        state_path = self.paths.metadata_dir / "enrichment_state.json"
        identity = VerifiedTargetContext.from_scope(scope).model_dump(mode="json")
        if state_path.exists():
            state = json.loads(state_path.read_bytes())
            if state.get("identity") != identity or state.get("options") != options:
                raise ValueError("enrichment_checkpoint_conflict")
            if state["phase"] in {"crawling", "processing", "requires_inspection"}:
                raise ValueError("requires_inspection")
            return state
        state = dict(enrichment_run_id=str(uuid4()), discovery_run_id=str(uuid4()), identity=identity,
                     options=options, phase="discovery", attempted_urls=[], crawl_runs=[])
        for source, entity, raw in (("pypi", "packages", scope.pypi), ("github", "repositories", scope.github)):
            target = self.paths.raw_path(source, entity)
            if target.exists():
                if self.storage.read_contract_records(target) != [raw]:
                    raise ValueError("enrichment_identity_copy_mismatch")
            else:
                self.storage.append_contract_records_durable(target, [raw])
        self.save(state)
        return state

    def save(self, state: dict) -> None:
        write_json_atomic(self.paths.metadata_dir / "enrichment_state.json", state)

    def discover(self, scope: RepositoryScopeEvidence, provider: str, options: dict,
                 *, seed: Path | None = None) -> tuple[dict, list[RawRecord]]:
        state = self.prepare(scope, options)
        if seed is not None and not state.get("seeded"):
            # Copy the immutable job snapshot verbatim; its original execution scope is revalidated.
            saved = json.loads(seed.read_bytes())
            run_id = json.loads((seed.parent / f"run_{seed.stem.removeprefix('discovery_state_')}.json").read_bytes())["run_id"]
            state["discovery_run_id"] = run_id
            directory = self.paths.metadata_dir / "collection_runs"
            write_json_atomic(directory / seed.name, saved)
            write_json_atomic(directory / f"run_{run_id}.json", json.loads((seed.parent / f"run_{run_id}.json").read_bytes()))
            state["seeded"] = True
            state["deferred_sources"] = [name for name, policy in saved["scope"]["providers"].items()
                                          if not policy.get("is_available", True)] if "providers" in saved["scope"] else []
            self.save(state)
        run_id = state["discovery_run_id"]
        checkpoint = RunManager(self.paths.metadata_dir / "collection_runs").checkpoints_dir / f"discovery_state_{run_id}.json"
        if state.get("discovery_status") not in TERMINAL_PROVIDER_STATES:
            with (HTTPClient() if self.http is None else nullcontext(self.http)) as http:
                with self.discovery_factory(http_client=http, storage_paths=self.paths) as discovery:
                    try:
                        result = discovery.discover(scope.github, package_id=scope.pypi.package_id,
                            run_id=run_id, resume=checkpoint.exists(), extended_package_queries=True,
                            policy_budgeted=True, enabled_sources=[provider], sources=list(EXTERNAL_SOURCES),
                            deferred_sources=state.get("deferred_sources", []), reactivate_sources=[provider],
                            max_pages=options["discovery_max_pages"], max_records=options["discovery_max_records"])
                    except ValueError as error:
                        if checkpoint.exists():
                            raise ValueError("discovery_checkpoint_scope_mismatch") from error
                        raise
            jobs = [job for job in result.discovery_manifest["per_job_stats"] if job["provider"] == provider]
            state.update(jobs=jobs, discovery_status=discovery_status(jobs), projection_path=str(checkpoint))
            # The checkpoint retains complete lineage; the provider artifact exposes only its own observations.
            records = [raw for raw in result.collection_result.records if raw.payload["discovery_provider"] == provider]
            if len(records) >= options["discovery_max_records"] // len(EXTERNAL_SOURCES):
                state["discovery_status"] = ProviderState.BUDGET
            target = self.paths.raw_path("external", "discovery")
            self.storage.write(target, records)
            self.save(state)
        records = self.storage.read_contract_records(self.paths.raw_dir / "external/discovery.jsonl")
        return state, records

    def capture(self, scope: RepositoryScopeEvidence, provider: str, options: dict,
                candidates: list[RawRecord], reused: list[RawRecord], *, projection_paths=(), document_associations=None, deferred_urls=(),
                selection_limited: bool = False) -> dict:
        state = self.prepare(scope, options)
        if state["phase"] == "complete":
            return json.loads((self.paths.reports_dir / "external_enrichment.json").read_bytes())
        document_path = self.paths.raw_path("external", "documents")
        stored = {raw.raw_record_id: raw for raw in self.storage.read_contract_records(document_path)}
        for raw in reused:
            previous = stored.get(raw.raw_record_id)
            if previous is not None and previous != raw:
                raise ValueError("conflicting_raw_record_id")
            if previous is None:
                self.storage.append_contract_record(document_path, raw)
                stored[raw.raw_record_id] = raw
        if candidates:
            state["phase"] = "crawling"
            self.save(state)
            with (HTTPClient() if self.http is None else nullcontext(self.http)) as http:
                with self.crawl_factory(http_client=http, storage_paths=self.paths,
                        repository_evidence=[scope], policy=self.url_policy) as crawler:
                    capture = crawler.crawl(candidates, max_candidates=min(20, len({raw.payload["candidate_url"] for raw in candidates})),
                                            discovery_state_paths=projection_paths)
            manifest = json.loads(capture.manifest_path.read_bytes())
            state["attempted_urls"] = sorted(set(state["attempted_urls"]) | {group["url"] for group in manifest["candidate_groups"]})
            state["crawl_runs"].append(str(capture.manifest_path))
            state["capture_errors"] = [error.model_dump(mode="json") for error in capture.collection_result.errors]
            state["retry_urls"] = [group["url"] for group in manifest["candidate_groups"]
                                   if group["errors"] and all(error.get("retryable") for error in group["errors"])]
            stored.update({raw.raw_record_id: raw for raw in capture.collection_result.records})
            state["phase"] = "discovery"
            self.save(state)
        if not candidates and not deferred_urls:
            state["capture_errors"], state["retry_urls"] = [], []
        if deferred_urls:
            state["capture_errors"] = state.get("capture_errors", []) + [dict(code="network_error", retryable=True,
                message="shared_capture_deferred") for _ in deferred_urls]
            state["retry_urls"] = sorted(set(state.get("retry_urls", [])) | set(deferred_urls))
        self.save(state)
        document_path.touch(exist_ok=True)
        status = state["discovery_status"]
        if state.get("capture_errors"):
            status = (ProviderState.QUOTA if state["discovery_status"] == ProviderState.QUOTA or any(error["code"] == "rate_limited" for error in state["capture_errors"])
                      else ProviderState.RETRY if all(error.get("retryable") for error in state["capture_errors"]) else ProviderState.INSPECT)
        if status == ProviderState.EMPTY and self.storage.read_contract_records(self.paths.raw_dir / "external/discovery.jsonl"):
            status = ProviderState.COMPLETE
        if status in {ProviderState.COMPLETE, ProviderState.EMPTY} and selection_limited:
            status = ProviderState.PENDING
        records = self.storage.read_contract_records(self.paths.raw_dir / "external/discovery.jsonl")
        report = dict(enrichment_run_id=state["enrichment_run_id"], status=status, provider=provider,
            candidates_discovered=len({raw.payload["candidate_id"] for raw in records}),
            documents_extracted=sum(raw.payload["content_status"] == "full" for raw in stored.values()),
            relevant_documents=0, irrelevant_documents=0, unresolved_documents=0,
            attempted_urls=state["attempted_urls"], document_raw_ids=sorted(stored),
            candidate_raw_ids=[raw.raw_record_id for raw in records],
            document_associations={raw.raw_record_id: sorted(set((document_associations or {}).get(raw.raw_record_id, [])) | {candidate.raw_record_id for candidate in records
                                      if candidate.payload["candidate_url"] == raw.source_url}) for raw in stored.values()},
            providers=self._provider_stats(state.get("jobs", [])),
            failures=[dict(stage="discovery", code=job.get("missing_reason")) for job in state.get("jobs", [])
                      if job.get("errors")] + state.get("capture_errors", []), temporal_limitations=["observation_only", "historical_availability_unverified"])
        metrics = report["providers"].setdefault(provider, dict(requests_attempted=0, pages_fetched=0,
            candidates_discovered=len(records), failures=0, missing_reasons=[]))
        reused_ids = {raw.raw_record_id for raw in reused}
        metrics.update(candidates_selected=len({canonical_article_url(raw.payload["candidate_url"]) for raw in records
                                                if canonical_article_url(raw.payload["candidate_url"]) in set(state["attempted_urls"])
                                                or any(canonical_article_url(doc.source_url) == canonical_article_url(raw.payload["candidate_url"]) for doc in stored.values())}),
                       documents_fetched=len(set(stored) - reused_ids), documents_reused=len(reused_ids),
                       documents_extracted=sum(raw.payload["content_status"] == "full" and raw.raw_record_id not in reused_ids for raw in stored.values()))
        if status in TERMINAL_PROVIDER_STATES:
            state["phase"] = "processing"
            self.save(state)
            if stored:
                contexts = [VerifiedTargetContext.from_scope(scope)]
                normalized = NormalizationPipeline().process(list(stored.values()), targets=contexts)
                for decision, count in normalized.processing_summary.relevance_decisions.items():
                    report[decision + "_documents"] = count
                processed = ProcessedDatasetPipeline(self.paths).run(self.paths.processing_inputs(), targets=contexts)
                if processed.status != "complete":
                    raise ValueError("requires_inspection")
                report["processing_run_id"] = processed.processing_run_id
            self.paths.processed_dir.mkdir(parents=True, exist_ok=True)
            state["phase"] = "complete"
            self.save(state)
        write_json_atomic(self.paths.reports_dir / "external_enrichment.json", report)
        return report

    @staticmethod
    def _provider_stats(jobs: list[dict]) -> dict:
        result = {}
        for job in jobs:
            stats = result.setdefault(job["provider"], dict(requests_attempted=0, pages_fetched=0,
                candidates_discovered=0, candidates_selected=0, documents_fetched=0,
                documents_extracted=0, failures=0, missing_reasons=[]))
            stats["requests_attempted"] += job["requests_attempted"]
            stats["pages_fetched"] += job["pages_fetched"]
            stats["candidates_discovered"] = len({key for entry in jobs for key in entry["candidate_ids"]})
            stats["failures"] += len(job["errors"])
            if job.get("missing_reason"):
                stats["missing_reasons"] = sorted(set(stats["missing_reasons"] + [job["missing_reason"]]))
        return result
