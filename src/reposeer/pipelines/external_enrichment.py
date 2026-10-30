"""External-only orchestration over immutable, previously verified identity observations."""

import json
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.collection.run_manager import RunManager
from reposeer.collectors.external.crawler.scope import RepositoryScopeEvidence
from reposeer.contracts import RawRecord, validate_record
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


class ExternalEnrichmentPipeline:
    """Reuse discovery, content capture and relevance, without live identity collectors.

    Discovery resumes its durable job state. An interrupted content or processing
    write requires inspection, since those pipelines do not expose safe cursor resume.
    """

    def __init__(self, storage_paths: StoragePaths, *, http_client: HTTPClient | None = None,
                 discovery_factory=ExternalPipeline, crawl_factory=ExternalCrawlPipeline,
                 url_policy=None):
        self.paths = storage_paths
        self.http = http_client
        self.discovery_factory = discovery_factory
        self.crawl_factory = crawl_factory
        self.url_policy = url_policy

    def run(self, scopes: Sequence[RepositoryScopeEvidence], options: dict) -> dict:
        state_path = self.paths.metadata_dir / "enrichment_state.json"
        report_path = self.paths.reports_dir / "external_enrichment.json"
        storage = JsonlStorage()
        contexts = [VerifiedTargetContext.from_scope(scope) for scope in scopes]
        identity = [context.model_dump(mode="json") for context in contexts]
        if state_path.exists():
            state = json.loads(state_path.read_bytes())
            if state["identity"] != identity or state["options"] != options:
                raise ValueError("enrichment_checkpoint_conflict")
            if state["phase"] == "complete":
                return json.loads(report_path.read_bytes())
            if state["phase"] in {"crawling", "processing", "requires_inspection"}:
                raise ValueError("requires_inspection")
        else:
            state = dict(enrichment_run_id=str(uuid4()), identity=identity, options=options,
                         phase="discovery", runs={}, discoveries={})
            write_json_atomic(state_path, state)
        # Minimal copied identity records retain original raw IDs/timestamps. No activity is copied.
        for source, observations in (("pypi", [scope.pypi for scope in scopes]),
                                     ("github", [scope.github for scope in scopes])):
            target = self.paths.raw_path(source, "packages" if source == "pypi" else "repositories")
            unique = {raw.raw_record_id: raw for raw in observations}
            if target.exists():
                saved = {raw.raw_record_id: raw for raw in storage.read_contract_records(target)}
                if saved != unique:
                    raise ValueError("enrichment_identity_copy_mismatch")
            else:
                storage.append_contract_records_durable(target, list(unique.values()))
        report = dict(enrichment_run_id=state["enrichment_run_id"], status="complete",
                      candidates_discovered=0, documents_extracted=0, relevant_documents=0,
                      irrelevant_documents=0, unresolved_documents=0, providers={}, failures=[],
                      temporal_limitations=["current_capture_observation_only", "historical_availability_unverified",
                                            "rss_configured_feed_entries_only", "news_plan_limited_history"],
                      missing_reasons=["function_level_profile_not_provided"])
        with (HTTPClient() if self.http is None else nullcontext(self.http)) as http:
            for scope in scopes:
                package = scope.pypi.package_id
                if package in state["discoveries"]:
                    continue
                run_id = state["runs"].setdefault(package, str(uuid4()))
                write_json_atomic(state_path, state)
                checkpoint = RunManager(self.paths.metadata_dir / "collection_runs").checkpoints_dir / f"discovery_state_{run_id}.json"
                with self.discovery_factory(http_client=http, storage_paths=self.paths) as discovery:
                    result = discovery.discover(scope.github, package_id=package, run_id=run_id,
                                                resume=checkpoint.exists(), extended_package_queries=True,
                                                sources=options["discovery_sources"],
                                                max_pages=options["discovery_max_pages"],
                                                max_records=options["discovery_max_records"])
                jobs = [job for job in result.discovery_manifest["per_job_stats"]
                        if job["provider"] in options["discovery_sources"]]
                errors = [dict(provider=job["provider"], reason=job.get("missing_reason"),
                               error_codes=[error["code"] for error in job["errors"]])
                          for job in jobs if job["errors"] or job["status"] in {"failed", "unavailable"}]
                attempted = {job["provider"] for job in jobs if job["attempted"]}
                errors.extend(dict(provider=name, reason="provider_not_attempted")
                              for name in options["discovery_sources"] if name not in attempted)
                if errors:
                    report.update(status="partial", failures=errors)
                    report["providers"] = self._provider_stats(jobs)
                    report["candidates_discovered"] = len(result.collection_result.records)
                    write_json_atomic(report_path, report)
                    return report
                state["discoveries"][package] = dict(run_id=result.run_id, jobs=jobs,
                    projection_path=result.discovery_manifest.get("candidate_projection_path"))
                write_json_atomic(state_path, state)
            all_jobs = [job for entry in state["discoveries"].values() for job in entry["jobs"]]
            report["providers"] = self._provider_stats(all_jobs)
            candidate_path = self.paths.raw_path("external", "discovery")
            candidate_path.touch(exist_ok=True)
            candidates = storage.read_contract_records(candidate_path)
            report["candidates_discovered"] = len({raw.payload["candidate_id"] for raw in candidates})
            document_path = self.paths.raw_path("external", "documents")
            if candidates:
                state["phase"] = "crawling"
                write_json_atomic(state_path, state)
                with self.crawl_factory(http_client=http, storage_paths=self.paths,
                                        repository_evidence=scopes, policy=self.url_policy) as crawler:
                    capture = crawler.crawl(candidates, max_candidates=options["max_candidates"],
                        discovery_state_paths=[Path(entry["projection_path"]) for entry in
                                               state["discoveries"].values() if entry["projection_path"]])
                crawl_manifest = json.loads(capture.manifest_path.read_bytes())
                for group in crawl_manifest["candidate_groups"]:
                    providers = {ref["discovery_provider"] for ref in group["discovery_references"]}
                    for name in providers:
                        stats = report["providers"][name]
                        stats["candidates_selected"] += 1
                        stats["failures"] += len(group["errors"])
                documents = capture.collection_result.records
                for raw in documents:
                    providers = {ref["discovery_provider"] for ref in raw.payload["discovery_references"]}
                    for name in providers:
                        stats = report["providers"][name]
                        stats["documents_fetched"] += 1
                        stats["documents_extracted"] += int(raw.payload["content_status"] == "full")
                report["documents_extracted"] = sum(raw.payload["content_status"] == "full" for raw in documents)
                report["failures"].extend(dict(stage="crawl", code=error.code) for error in capture.collection_result.errors)
                if capture.collection_result.errors:
                    report["status"] = "partial"
            else:
                documents = []
                report["status"] = "no_candidates"
            document_path.touch(exist_ok=True)
            state["phase"] = "processing"
            write_json_atomic(state_path, state)
            if documents:
                normalized = NormalizationPipeline().process(documents, targets=contexts)
                for decision, count in normalized.processing_summary.relevance_decisions.items():
                    report[decision + "_documents"] = count
                processed = ProcessedDatasetPipeline(self.paths).run(self.paths.processing_inputs(), targets=contexts)
                report["processing_run_id"] = processed.processing_run_id
                if processed.status != "complete":
                    report["status"] = "partial"
                    report["failures"].append(dict(stage="processing", code="processing_partial"))
            self.paths.processed_dir.mkdir(parents=True, exist_ok=True)
            write_json_atomic(report_path, report)
            state["phase"] = "complete" if report["status"] in {"complete", "no_candidates"} else "requires_inspection"
            write_json_atomic(state_path, state)
            return report

    @staticmethod
    def _provider_stats(jobs: list[dict]) -> dict:
        result, candidates = {}, {}
        for job in jobs:
            stats = result.setdefault(job["provider"], dict(requests_attempted=0, pages_fetched=0,
                candidates_discovered=0, candidates_selected=0, documents_fetched=0,
                documents_extracted=0, failures=0, missing_reasons=[]))
            # One failed attempt is retained even when no page was fetched.
            stats["requests_attempted"] += job["requests_attempted"]
            stats["pages_fetched"] += job["pages_fetched"]
            candidates.setdefault(job["provider"], set()).update(job["candidate_ids"])
            stats["candidates_discovered"] = len(candidates[job["provider"]])
            stats["failures"] += len(job["errors"]) + int(job["status"] == "unavailable")
            if job.get("missing_reason"):
                stats["missing_reasons"] = sorted(set(stats["missing_reasons"] + [job["missing_reason"]]))
        return result

