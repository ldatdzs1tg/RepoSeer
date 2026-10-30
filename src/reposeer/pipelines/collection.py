"""T-020 integration; collectors retain their original contracts, runs and checkpoints."""

import json
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import polars as pl
from pydantic import BaseModel

from reposeer.clients.github import GitHubClient
from reposeer.clients.pypi import PyPIClient
from reposeer.collection.http.cache import ResponseCache
from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.collectors.external.crawler.scope import RepositoryScopeEvidence
from reposeer.collectors.package.repository_mapping import PackageRepositoryResolver, _root_url
from reposeer.config import settings
from reposeer.contracts import GitHubRepository, RawRecord, validate_record
from reposeer.contracts.identity import checked_timestamp, format_utc_timestamp, package_id
from reposeer.exceptions import CollectionError, StorageError
from reposeer.pipelines.external import ExternalPipeline
from reposeer.pipelines.external_crawl import ExternalCrawlPipeline
from reposeer.pipelines.external_enrichment import provider_deferrals
from reposeer.pipelines.normalization import NormalizationPipeline
from reposeer.pipelines.processed import ProcessedDatasetPipeline
from reposeer.processing.relevance import VerifiedTargetContext
from reposeer.profiling.report import LIMITATIONS, ProfilingReportGenerator
from reposeer.services.activity_batch_service import (
    ActivityBatchService,
    validate_budgets,
    validate_groups,
)
from reposeer.services.repository_service import parse_repository_target
from reposeer.storage.manifest import ProcessingManifest
from reposeer.storage.paths import StoragePaths
from reposeer.storage.raw.jsonl import JsonlStorage
from reposeer.technology import TechnologyDetector

Mode = Literal["live", "stored"]
EXPECTED_ERRORS = (OSError, ValueError, TypeError, CollectionError, StorageError, pl.exceptions.PolarsError)


class CollectionPipelineResult(BaseModel):
    integration_run_id: str
    status: Literal["complete", "partial", "failed"]
    mode: Mode
    targets_requested: int
    targets_resolved: int
    repositories_collected: int
    child_run_ids: dict[str, list[str]]
    processing_run_id: str | None
    processing_status: str
    manifest_path: Path
    collection_report_path: Path
    data_profile_path: Path
    errors: list[dict[str, str | None]]
    missing_reasons: list[str]


def parse_targets(targets: Sequence[str]) -> list[dict[str, Any]]:
    """Validate every target before constructing network clients; retain input occurrences."""
    if isinstance(targets, str | bytes) or not 1 <= len(targets) <= 100:
        raise ValueError("integration_requires_1_to_100_targets")
    result = []
    for value in targets:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid_integration_target")
        value = value.strip()
        subject = package_id(value[5:]) if value.startswith("pypi:") else None
        slug = None if subject else parse_repository_target(value)
        result.append(dict(
            input=value, normalized_target=subject or slug.casefold(),
            target_type="pypi" if subject else "github", package_id=subject,
            repo_full_name=slug, repo_id=None,
            mapping_status="unresolved" if subject else "not_applicable",
            mapping_evidence_raw_ids=[], evidence_observed_at={}, repository_raw_record_id=None,
            repository_observed_at=None, status="partial",
            missing_reasons=[], github_metadata_status="not_requested",
            activity_group_statuses=[], activity_run_id=None,
            discovery_status="not_requested", crawler_status="not_requested"))
    return result


def validate_configuration(*, mode: str, groups: Sequence[str], max_pages: int, max_records: int,
                           discovery_sources: Sequence[str], discovery_max_pages: int,
                           discovery_max_records: int, max_candidates: int, cutoff_at: str | None) -> None:
    if mode not in {"live", "stored"}:
        raise ValueError("unsupported_integration_mode")
    validate_groups(groups)
    validate_budgets(max_pages, max_records)
    validate_budgets(discovery_max_pages, discovery_max_records)
    if type(max_candidates) is not int or not 1 <= max_candidates <= 1000:
        raise ValueError("max_candidates_must_be_between_1_and_1000")
    allowed = {"hackernews", "devto", "stackexchange", "stackoverflow", "news", "newsapi", "rss"}
    if (isinstance(discovery_sources, str | bytes) or not discovery_sources
            or any(source not in allowed for source in discovery_sources)
            or len(discovery_sources) != len(set(discovery_sources))):
        raise ValueError("unsupported_discovery_sources")
    if cutoff_at is not None:
        checked_timestamp(cutoff_at)
        if mode != "stored":
            raise ValueError("cutoff_at_requires_stored_diagnostics")


def _repository_candidate(raw: RawRecord) -> tuple[str | None, str, str | None]:
    info = raw.payload.get("info")
    urls = info.get("project_urls") if isinstance(info, dict) else None
    if not isinstance(urls, dict):
        return None, "unresolved", "missing_source_evidence"
    roots = set()
    try:
        if package_id(info.get("name")) != raw.package_id:
            return None, "unresolved", "package_identity_mismatch"
        for label, url in urls.items():
            if label.strip().casefold() in {"source", "repository", "code"}:
                roots.add(_root_url(url))
    except (ValueError, TypeError, AttributeError):
        return None, "unresolved", "invalid_source_url"
    if len(roots) > 1:
        return None, "ambiguous", "conflicting_source_urls"
    if not roots:
        return None, "unresolved", "missing_source_evidence"
    return parse_repository_target(roots.pop()), "unresolved", None


def _discovery_status(jobs: Sequence[dict[str, Any]]) -> str:
    if not jobs:
        return "skipped"
    states = {job["status"] for job in jobs}
    if states == {"complete"}:
        return "complete"
    return "partial" if states & {"complete", "partial", "skipped_budget", "pending", "running"} else "failed"


class CollectionPipeline:
    def __init__(self, storage_paths: StoragePaths, *,
                 pypi_factory: Callable[..., PyPIClient] = PyPIClient,
                 activity_factory: Callable[..., ActivityBatchService] = ActivityBatchService,
                 discovery_factory: Callable[..., ExternalPipeline] = ExternalPipeline,
                 crawl_factory: Callable[..., ExternalCrawlPipeline] = ExternalCrawlPipeline,
                 detector: TechnologyDetector | None = None,
                 profiler: ProfilingReportGenerator | None = None,
                 url_policy: URLPolicy | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.paths = storage_paths
        self.clock = clock or (lambda: datetime.now(UTC))
        self.pypi_factory, self.activity_factory = pypi_factory, activity_factory
        self.discovery_factory, self.crawl_factory = discovery_factory, crawl_factory
        self.detector = detector or TechnologyDetector(clock=self.clock)
        self.profiler = profiler or ProfilingReportGenerator(clock=self.clock)
        self.storage = JsonlStorage()
        self.url_policy = url_policy

    def _ref(self, path: Path) -> str:
        return path.resolve().relative_to(self.paths.base_dir.resolve()).as_posix()

    def _path(self, manifest: dict) -> Path:
        return self.paths.metadata_dir / "collection_runs" / f"integration_{manifest['integration_run_id']}.json"

    def _save(self, manifest: dict) -> None:
        write_json_atomic(self._path(manifest), manifest)

    def _stage(self, manifest: dict, target: str | None, stage: str, status: str, **kwargs: Any) -> None:
        manifest["stages"].append(dict(target=target, stage=stage, status=status,
                                       run_ids=[], manifest_paths=[], reason_codes=[], details={}))
        manifest["stages"][-1].update(kwargs)
        self._save(manifest)

    def _error(self, manifest: dict, target: str | None, stage: str, code: str, error: Exception) -> None:
        manifest["errors"].append(dict(target=target, stage=stage, code=code, error_type=type(error).__name__))
        self._stage(manifest, target, stage, "failed", reason_codes=[code])

    @staticmethod
    def _child(manifest: dict, kind: str, run_id: str) -> None:
        ids = manifest["child_run_ids"].setdefault(kind, [])
        if run_id not in ids:
            ids.append(run_id)

    @staticmethod
    def _source(manifest: dict, target: str, source: str, status: str, **metrics: Any) -> None:
        state = (status if status in {"skipped", "excluded"} else "failed" if status == "failed"
                 else "partial" if status != "complete" else
                 "completed_with_records" if metrics.get("records_emitted") else "attempted_no_results")
        entry = dict(target=target, source=source, state=state, attempted=int(status != "skipped"),
                     succeeded=int(status == "complete"), partial=int(status == "partial"),
                     failed=int(status == "failed"), skipped=int(status == "skipped"),
                     excluded=int(status == "excluded"),
                     records_emitted=None, records_stored=None, records_rejected=None,
                     pages_fetched=None, pagination_complete=None, observed_candidate_count=None,
                     crawled_candidate_count=None, candidates_attempted=None, documents_extracted=None,
                     content_unavailable=None, fetch_failed=None, other_capture_failed=None)
        entry.update(metrics)
        manifest["sources"].append(entry)

    def _pypi(self, manifest: dict, targets: list[dict], stack: ExitStack, http: HTTPClient | None) -> dict[str, RawRecord]:
        saved = (self.storage.read_contract_records(self.paths.raw_dir / "pypi/packages.jsonl")
                 if manifest["mode"] == "stored" else [])
        records, client = {}, None
        for target in targets:
            if target["target_type"] != "pypi":
                continue
            key, subject = target["normalized_target"], target["package_id"]
            try:
                if manifest["mode"] == "live":
                    if client is None:
                        client = self.pypi_factory(http_client=http) if self.pypi_factory is PyPIClient else self.pypi_factory()
                        stack.callback(client.close)
                    payload = client.get_package_info(subject[5:])
                    observed = format_utc_timestamp(self.clock())
                    raw = RawRecord(
                        schema_version="0.1.0", record_type="raw_record", raw_record_id=f"raw:{uuid4()}",
                        run_id=str(uuid4()), source="pypi_json", source_record_type="dependency_metadata",
                        source_record_id=subject[5:], repo_id=None, package_id=subject,
                        source_url=f"https://pypi.org/pypi/{subject[5:]}/json",
                        retrieved_at=observed, available_at=observed, event_at=None,
                        http_status=None, raw_body_sha256=None, payload=payload)
                    self.storage.append_contract_records_durable(self.paths.raw_path("pypi", "packages"), [raw])
                    self._child(manifest, "pypi", raw.run_id)
                    self._source(manifest, key, "pypi_json", "complete", records_emitted=1, records_stored=1)
                else:
                    eligible = [raw for raw in saved if raw.source == "pypi_json"
                                and raw.package_id == subject
                                and raw.source_url == f"https://pypi.org/pypi/{subject[5:]}/json"]
                    raw = max(eligible, key=lambda raw: (raw.retrieved_at, raw.raw_record_id), default=None)
                    if raw is None:
                        target["missing_reasons"].append("missing_stored_evidence")
                        continue
                records[key] = raw
                slug, status, reason = _repository_candidate(raw)
                target.update(repo_full_name=slug, mapping_status=status,
                              mapping_evidence_raw_ids=[raw.raw_record_id],
                              evidence_observed_at={raw.raw_record_id: raw.retrieved_at})
                if reason:
                    target["missing_reasons"].append(reason)
                self._save(manifest)
            except EXPECTED_ERRORS as error:
                target["status"] = "failed"
                target["missing_reasons"].append("pypi_evidence_unavailable")
                self._error(manifest, key, "identity_resolution", "pypi_evidence_unavailable", error)
                if manifest["mode"] == "live":
                    self._source(manifest, key, "pypi_json", "failed")
        return records

    def _github(self, manifest: dict, targets: list[dict], stack: ExitStack,
                groups: Sequence[str], max_pages: int, max_records: int,
                http: HTTPClient | None) -> dict[str, tuple[GitHubRepository, RawRecord]]:
        slugs = list({target["repo_full_name"].casefold(): target["repo_full_name"]
                      for target in targets if target["repo_full_name"]}.values())
        repositories = {}
        if manifest["mode"] == "stored":
            records = self.storage.read_contract_records(self.paths.raw_dir / "github/repositories.jsonl")
            for slug in slugs:
                candidates = [raw for raw in records if raw.source_record_type == "github_repository"
                              and str(raw.payload.get("full_name", "")).casefold() == slug.casefold()]
                raw = max(candidates, key=lambda raw: (raw.retrieved_at, raw.raw_record_id), default=None)
                if raw is not None:
                    normalized = NormalizationPipeline().process([raw])
                    if normalized.canonical_repositories:
                        repositories[slug.casefold()] = normalized.canonical_repositories[0], raw
            for target in targets:
                self._stage(manifest, target["normalized_target"], "github_collection", "skipped",
                            reason_codes=["stored_mode_no_network"])
                target["github_metadata_status"] = "stored_evidence" if (
                    (target["repo_full_name"] or "").casefold() in repositories) else "missing_stored_evidence"
            return repositories
        if not slugs:
            return repositories

        def started(run_id: str, path: Path) -> None:
            self._child(manifest, "github_activity", run_id)
            self._stage(manifest, None, "github_collection", "partial", run_ids=[run_id],
                        manifest_paths=[self._ref(path)], reason_codes=["child_run_started"])

        options = dict(storage_paths=self.paths, clock=self.clock, on_run_started=started)
        if self.activity_factory is ActivityBatchService:
            options["client"] = GitHubClient(http_client=http)
        service = self.activity_factory(**options)
        stack.callback(service.close)
        previous_raw_ids = {raw.raw_record_id for raw in self.storage.read_contract_records(
            self.paths.raw_dir / "github/repositories.jsonl")}
        batch = service.start(slugs, groups=groups, max_pages_per_group=max_pages,
                              max_records_per_group=max_records)
        self._child(manifest, "github_activity", batch.run_id)
        context = service.run_manager.resume_run(batch.run_id)
        raw_by_id = {raw.raw_record_id: raw for raw in self.storage.read_contract_records(
            self.paths.raw_dir / "github/repositories.jsonl")}
        for raw_id, observation in raw_by_id.items():
            if raw_id not in previous_raw_ids:
                self._child(manifest, "github_metadata", observation.run_id)
        for index, item in enumerate(batch.items):
            if item.status in {"processed", "duplicate"}:
                repository = validate_record(context.checkpoint_state[f"activity:repo:{index}:context"])
                if not isinstance(repository, GitHubRepository):
                    raise ValueError("invalid_verified_repository_context")
                raw = raw_by_id.get(repository.provenance.raw_record_ids[0])
                if raw is None or raw.repo_id != repository.identity.repo_id:
                    raise ValueError("metadata_evidence_identity_mismatch")
                repositories[item.target.casefold()] = repository, raw
                self._child(manifest, "github_metadata", raw.run_id)
                self._source(manifest, item.target, "github_metadata", "complete",
                             records_emitted=1, records_stored=1, raw_record_ids=[raw.raw_record_id], run_id=raw.run_id,
                             repo_id=raw.repo_id)
            else:
                self._source(manifest, item.target, "github_metadata", item.status, error_code=item.error_code)
            entries = [dict(entry) for entry in batch.groups
                       if entry["repo_full_name"].casefold() == item.target.casefold()]
            if item.status != "duplicate":
                for entry in entries:
                    status = ("skipped" if entry["missing_reason"] in {"metadata_unavailable", "excluded", "duplicate"}
                              else "complete" if entry["status"] == "complete"
                              else "partial" if entry["status"] == "partial" or entry["records_stored"] else "failed")
                    metrics = {key: value for key, value in entry.items() if key != "status"}
                    self._source(manifest, item.target, "github_" + entry["group"], status,
                                 native_status=entry["status"], **metrics)
            for target in targets:
                if target["repo_full_name"] and target["repo_full_name"].casefold() == item.target.casefold():
                    target.update(github_metadata_status=item.status, activity_group_statuses=entries,
                                  activity_run_id=batch.run_id)
                    if item.status in {"excluded", "failed"}:
                        target["status"] = item.status
            repo_status = ("complete" if item.status in {"processed", "duplicate"}
                           and (item.status == "duplicate" or all(entry["status"] == "complete" for entry in entries))
                           else "partial" if item.status == "processed"
                           else "skipped" if item.status == "excluded" else "failed")
            reasons = (["repository_excluded"] if item.status == "excluded" else
                       sorted({entry["missing_reason"] for entry in entries if entry["missing_reason"]}))
            self._stage(manifest, item.target, "github_collection", repo_status,
                        run_ids=[batch.run_id], manifest_paths=[self._ref(batch.report_path)],
                        reason_codes=reasons,
                        details=dict(metadata_status=item.status, error_code=item.error_code, batch_status=batch.status,
                                     activity_groups=entries, resumable=batch.resumable,
                                     checkpoint_path=self._ref(batch.checkpoint_path)))
        return repositories

    def _resolve(self, manifest: dict, targets: list[dict], packages: dict[str, RawRecord],
                 repositories: dict[str, tuple[GitHubRepository, RawRecord]]) -> dict[str, RepositoryScopeEvidence]:
        scopes = {}
        for target in targets:
            evidence = repositories.get((target["repo_full_name"] or "").casefold())
            if evidence:
                repository, raw = evidence
                target.update(repository_raw_record_id=raw.raw_record_id, repository_observed_at=raw.retrieved_at)
                target["repo_id"] = repository.identity.repo_id if target["target_type"] == "github" else None
                if target["target_type"] == "pypi":
                    pypi = packages[target["normalized_target"]]
                    mapping = PackageRepositoryResolver().resolve_current(target["package_id"][5:], pypi.payload, repository)
                    target["mapping_status"] = mapping.status
                    if mapping.status == "verified_current":
                        scope = RepositoryScopeEvidence(pypi=pypi, github=raw)
                        try:
                            scope.resolve()
                        except EXPECTED_ERRORS as error:
                            target["missing_reasons"].append("invalid_mapping_evidence")
                            target["status"] = "failed"
                            self._error(manifest, target["normalized_target"], "identity_resolution",
                                        "invalid_mapping_evidence", error)
                            continue
                        scopes[target["normalized_target"]] = scope
                        target.update(repo_id=mapping.repo_id,
                                      mapping_evidence_raw_ids=[pypi.raw_record_id, raw.raw_record_id])
                        target["evidence_observed_at"][raw.raw_record_id] = raw.retrieved_at
                    elif mapping.reason:
                        target["missing_reasons"].append(mapping.reason)
                target["status"] = "complete" if target["repo_id"] else "partial"
            elif target["github_metadata_status"] == "excluded":
                target["status"] = "excluded"
                target["missing_reasons"].append("repository_excluded")
            elif not target["missing_reasons"]:
                target["missing_reasons"].append("missing_stored_evidence" if manifest["mode"] == "stored"
                                                 else "github_metadata_unavailable")
            self._stage(manifest, target["normalized_target"], "identity_resolution",
                        "skipped" if target["status"] == "excluded" else target["status"],
                        reason_codes=target["missing_reasons"],
                        details=dict(evidence_observed_at=target["evidence_observed_at"],
                                     mapping_status=target["mapping_status"]))
        return scopes

    def _external(self, manifest: dict, target: dict, raw: RawRecord,
                  scope: RepositoryScopeEvidence | None, stack: ExitStack,
                  sources: Sequence[str], pages: int, records: int, max_candidates: int,
                  http: HTTPClient | None) -> None:
        key = target["normalized_target"]
        stage = "technology_resolution"
        try:
            detection = self.detector.detect(raw)
            self._stage(manifest, key, "technology_resolution", "complete",
                        details=dict(detection=detection.model_dump(mode="json"),
                                     missing_reasons=["readme_manifest_not_collected"]))
            if manifest["mode"] == "stored":
                for stage in ("external_discovery", "external_crawl"):
                    self._stage(manifest, key, stage, "skipped", reason_codes=["stored_mode_no_network"])
                target["discovery_status"] = target["crawler_status"] = "skipped"
                return
            stage = "external_discovery"
            options = dict(storage_paths=self.paths, clock=self.clock)
            if self.discovery_factory is ExternalPipeline:
                options["http_client"] = http
            discovery = self.discovery_factory(**options)
            stack.callback(discovery.close)
            result = discovery.discover(raw, package_id=target["package_id"] if scope else None,
                                        detection_result=detection, sources=sources, max_pages=pages, max_records=records,
                                        extended_package_queries=set(sources) == {"hackernews", "devto", "stackexchange", "rss", "news"},
                                        policy_budgeted=set(sources) == {"hackernews", "devto", "stackexchange", "rss", "news"},
                                        deferred_sources=manifest.get("deferred_external_sources", []))
            self._child(manifest, "external_discovery", result.run_id)
            collection, saved = result.collection_result, result.discovery_manifest
            aliases = {"stackoverflow": "stackexchange", "newsapi": "news"}
            requested = {aliases.get(source, source) for source in sources}
            jobs_requested = [job for job in saved["per_job_stats"] if job["provider"] in requested]
            if requested == {"hackernews", "devto", "stackexchange", "rss", "news"}:
                deferred = provider_deferrals(jobs_requested)
                if deferred:
                    manifest["deferred_external_sources"] = sorted(set(manifest.get("deferred_external_sources", [])) | deferred.keys())
            jobless = requested - {job["provider"] for job in jobs_requested}
            discovery_status = "partial" if jobless else _discovery_status(jobs_requested)
            target["discovery_status"] = discovery_status
            self._stage(manifest, key, "external_discovery", discovery_status, run_ids=[result.run_id],
                        manifest_paths=[self._ref(self.paths.metadata_dir / "collection_runs" / f"discovery_manifest_{result.run_id}.json")],
                        reason_codes=sorted({job["missing_reason"] for job in jobs_requested if job["missing_reason"]}
                                            | ({"no_planned_queries"} if jobless else set())),
                        details=dict(search_plan=result.search_plan.model_dump(mode="json"), discovery=saved,
                                     requested_sources=sorted(requested),
                                     upstream_status=collection.status,
                                     status_scope="explicitly_requested_sources"))
            aliases = {"stackoverflow": "stackexchange", "newsapi": "news"}
            for provider in dict.fromkeys(aliases.get(source, source) for source in sources):
                jobs = [entry for entry in saved["per_job_stats"] if entry["provider"] == provider]
                emitted = [record for record in collection.records if record.payload.get("discovery_provider") == provider]
                status = _discovery_status(jobs)
                states = {job["status"] for job in jobs}
                self._source(manifest, key, provider, status,
                             records_emitted=len(emitted), records_stored=len(emitted),
                             records_rejected=sum(job["records_rejected"] for job in jobs),
                             attempted=sum(job["attempted"] for job in jobs),
                             succeeded=sum(job["status"] == "complete" for job in jobs),
                             partial=sum(job["status"] == "partial" for job in jobs),
                             failed=sum(job["status"] == "failed" for job in jobs),
                             skipped=sum(job["status"] == "skipped_budget" for job in jobs) if jobs else 1,
                             state="unavailable" if jobs and all(job["status"] == "unavailable" for job in jobs)
                             else "skipped" if not jobs else
                             "completed_with_records" if status == "complete" and emitted else
                             "attempted_no_results" if status == "complete" else status,
                             native_status=(next(iter(states)) if len(states) == 1
                                            else status if jobs else None),
                             reason_code="no_planned_queries" if not jobs else None,
                             pages_fetched=sum(job["pages_fetched"] for job in jobs),
                             pagination_complete=(all(not job["has_more"] and job["status"] == "complete" for job in jobs)
                                                  if jobs else None),
                             observed_candidate_count=len({record.payload["candidate_id"] for record in emitted}),
                             provider_jobs=jobs)
            for provider in sorted({job["provider"] for job in saved["per_job_stats"]} - requested):
                self._source(manifest, key, provider, "skipped", state="not_requested", attempted=0, skipped=0,
                             provider_jobs=[job for job in saved["per_job_stats"] if job["provider"] == provider])
            if not collection.records:
                target["crawler_status"] = "skipped"
                self._stage(manifest, key, "external_crawl", "skipped", reason_codes=["no_discovery_candidates"])
                return
            stage = "external_crawl"
            options = dict(storage_paths=self.paths, clock=self.clock, repository_evidence=[scope] if scope else [])
            if self.crawl_factory is ExternalCrawlPipeline:
                options.update(http_client=http, policy=self.url_policy)
            crawler = self.crawl_factory(**options)
            stack.callback(crawler.close)
            state = saved.get("candidate_projection_path")
            crawled = crawler.crawl(collection.records, max_candidates=max_candidates,
                                    discovery_state_paths=[Path(state)] if state else [])
            self._child(manifest, "external_crawl", crawled.run_id)
            target["crawler_status"] = crawled.collection_result.status
            crawl_manifest = json.loads(crawled.manifest_path.read_text(encoding="utf-8"))
            captured = crawled.collection_result.records
            captured_by_id = {record.raw_record_id: record for record in captured}
            persisted = {record.raw_record_id: record for record in
                         self.storage.read_contract_records(crawled.output_path)
                         if record.raw_record_id in captured_by_id}
            extracted = sum(record.payload.get("content_status") == "full" for record in captured)
            unavailable = sum(record.payload.get("content_status") == "unavailable" for record in captured)
            if (persisted != captured_by_id or len(captured_by_id) != len(captured)
                    or set(crawl_manifest["raw_record_ids"]) != set(captured_by_id)
                    or crawl_manifest["documents_extracted"] != extracted
                    or extracted + unavailable != len(captured)
                    or any(record.source_record_type != "article"
                           or record.payload.get("record_stage") != "external_document" for record in captured)):
                raise ValueError("crawl_capture_metrics_mismatch")
            errors = crawled.collection_result.errors
            fetch_codes = {"not_found", "http_error", "network_error", "rate_limited"}
            # T-017 attaches at most one collection error per attempted candidate group.
            # Projection/other errors of a different granularity cannot establish a count.
            groups = crawl_manifest.get("candidate_groups", [])
            grouped_errors = [error for group in groups for error in group.get("errors", [])]
            distinct_failures_verified = (
                len(groups) == crawl_manifest["attempted_candidate_count"]
                and all(len(group.get("errors", [])) <= 1 for group in groups)
                and grouped_errors == [error.model_dump(mode="json") for error in errors]
                and all(error.code in fetch_codes | {"parse_error", "validation_error"} for error in errors))
            other_failed = (sum(error.code not in fetch_codes and error.raw_record_id not in captured_by_id
                                for error in errors) if distinct_failures_verified else None)
            self._stage(manifest, key, "external_crawl", crawled.collection_result.status, run_ids=[crawled.run_id],
                        manifest_paths=[self._ref(crawled.manifest_path)],
                        reason_codes=[error.code for error in errors], details=crawl_manifest)
            self._source(manifest, key, "external_crawl", crawled.collection_result.status,
                         records_emitted=len(captured), records_stored=len(captured),
                         records_rejected=crawled.collection_result.metadata.records_rejected,
                         observed_candidate_count=crawl_manifest["unique_candidate_count"],
                         crawled_candidate_count=crawl_manifest["attempted_candidate_count"],
                         candidates_attempted=crawl_manifest["attempted_candidate_count"],
                         documents_extracted=extracted, content_unavailable=unavailable,
                         fetch_failed=sum(error.code in fetch_codes for error in errors),
                         other_capture_failed=other_failed)
        except EXPECTED_ERRORS as error:
            if stage == "external_discovery":
                target["discovery_status"] = "failed"
            elif stage == "external_crawl":
                target["crawler_status"] = "failed"
            self._error(manifest, key, stage, "external_handoff_failed", error)

    def run(self, targets: Sequence[str], *, mode: Mode = "live", groups: Sequence[str] = ("all",),
            max_pages: int = 2, max_records: int = 200, discovery_sources: Sequence[str] = ("hackernews",),
            discovery_max_pages: int = 1, discovery_max_records: int = 30,
            max_candidates: int = 15, cutoff_at: str | None = None,
            available_external_sources: Sequence[str] | None = None) -> CollectionPipelineResult:
        parsed = parse_targets(targets)
        validate_configuration(mode=mode, groups=groups, max_pages=max_pages, max_records=max_records,
                               discovery_sources=discovery_sources, discovery_max_pages=discovery_max_pages,
                               discovery_max_records=discovery_max_records, max_candidates=max_candidates, cutoff_at=cutoff_at)
        unique = list({target["normalized_target"]: target for target in reversed(parsed)}.values())
        manifest = dict(manifest_version="1", integration_run_id=str(uuid4()), mode=mode, status="partial",
                        started_at=format_utc_timestamp(self.clock()), finished_at=None,
                        storage_root=str(self.paths.base_dir),
                        requested_targets=[target["input"] for target in parsed], targets=unique,
                        stages=[], child_run_ids={}, processing={}, reports={}, errors=[], sources=[],
                        limitations=[*LIMITATIONS, "Current mapping is repo-only, not package subdirectory scope.",
                                     "Processing replays all eligible root raw files; it is not new collection.",
                                     "Resume children with their existing CLI; integration has no resume engine."])
        self._save(manifest)
        if available_external_sources is not None:
            manifest["deferred_external_sources"] = [name for name in discovery_sources
                if name not in available_external_sources]
        scopes, repositories = {}, {}
        with ExitStack() as stack:
            try:
                http = None
                if mode == "live" and any((
                    self.pypi_factory is PyPIClient, self.activity_factory is ActivityBatchService,
                    self.discovery_factory is ExternalPipeline, self.crawl_factory is ExternalCrawlPipeline,
                )):
                    cache = (ResponseCache(self.paths.metadata_dir / "http_cache",
                                           ttl_seconds=settings.collection.cache_ttl_seconds)
                             if settings.collection.cache_enabled else None)
                    http = stack.enter_context(HTTPClient(cache=cache))
                packages = self._pypi(manifest, unique, stack, http)
                repositories = self._github(manifest, unique, stack, validate_groups(groups), max_pages, max_records, http)
                scopes = self._resolve(manifest, unique, packages, repositories)
                work = {}
                for target in unique:
                    if target["repo_id"]:
                        key = (target["repo_id"], target["package_id"] if target["normalized_target"] in scopes else None)
                        work.setdefault(key, target)
                mapped = {key[0] for key in work if key[1] is not None}
                for (repo_id, subject), target in work.items():
                    if subject is None and repo_id in mapped:
                        continue
                    try:
                        with ExitStack() as target_stack:
                            self._external(manifest, target, repositories[target["repo_full_name"].casefold()][1],
                                           scopes.get(target["normalized_target"]), target_stack, discovery_sources,
                                           discovery_max_pages, discovery_max_records, max_candidates, http)
                    except EXPECTED_ERRORS as error:
                        self._error(manifest, target["normalized_target"], "external_crawl",
                                    "external_handoff_failed", error)
            except EXPECTED_ERRORS as error:
                self._error(manifest, None, "github_collection", "collection_stage_failed", error)
        for target in unique:
            for stage in ("external_discovery", "external_crawl"):
                if not any(entry["stage"] == stage and entry["target"] == target["normalized_target"]
                           for entry in manifest["stages"]):
                    reason = ("stored_mode_no_network" if mode == "stored" else
                              "shared_repository_context" if target["repo_id"] else "identity_unresolved")
                    target["discovery_status" if stage == "external_discovery" else "crawler_status"] = "skipped"
                    self._stage(manifest, target["normalized_target"], stage, "skipped",
                                reason_codes=[reason])
        contexts = [VerifiedTargetContext.from_scope(scope) for scope in scopes.values()]
        processing = None
        before = set((self.paths.metadata_dir / "collection_runs").glob("processing_*.json"))
        try:
            result = ProcessedDatasetPipeline(self.paths).run(
                self.paths.processing_inputs(), targets=contexts, cutoff_at=cutoff_at)
            processing = ProcessingManifest.model_validate_json(result.manifest_path.read_bytes())
            manifest["processing"] = dict(
                processing_run_id=result.processing_run_id, status=result.status,
                manifest_path=self._ref(result.manifest_path), input_record_count=result.input_record_count,
                dataset_row_counts=result.dataset_row_counts)
            self._stage(manifest, None, "processing", result.status, run_ids=[result.processing_run_id],
                        manifest_paths=[self._ref(result.manifest_path)])
        except EXPECTED_ERRORS as error:
            added = set((self.paths.metadata_dir / "collection_runs").glob("processing_*.json")) - before
            if len(added) == 1:
                path = added.pop()
                processing = ProcessingManifest.model_validate_json(path.read_bytes())
                manifest["processing"] = dict(processing_run_id=processing.processing_run_id, status="failed",
                                             manifest_path=self._ref(path), input_record_count=None, dataset_row_counts=None)
            self._error(manifest, None, "processing", "processing_failed", error)
        crawls = [entry for entry in manifest["sources"] if entry["source"] == "external_crawl"]
        external = {
            key: sum(entry[key] for entry in crawls)
            if crawls and all(entry.get(key) is not None for entry in crawls) else None
            for key in ("candidates_attempted", "documents_extracted", "content_unavailable",
                        "fetch_failed", "other_capture_failed")}
        external["candidates_discovered"] = sum(
            stage["details"]["discovery"]["candidate_count"] for stage in manifest["stages"]
            if stage["stage"] == "external_discovery" and "discovery" in stage["details"]) if mode == "live" else None
        quality_path = self.paths.reports_dir / "data_quality" / f"data_profile_{manifest['integration_run_id']}.json"
        collection_path = self.paths.reports_dir / "data_quality" / f"collection_profile_{manifest['integration_run_id']}.json"
        manifest["reports"] = dict(collection_profile=self._ref(collection_path), data_profile=self._ref(quality_path))
        try:
            profile = self.profiler.profile_storage(self.paths, integration_run_id=manifest["integration_run_id"],
                                                   processing=processing, external_metrics=external)
            self.profiler.save_report(profile, quality_path)
            self._stage(manifest, None, "profiling", "complete", manifest_paths=[self._ref(quality_path)])
        except EXPECTED_ERRORS as error:
            self._error(manifest, None, "profiling", "profiling_failed", error)
        manifest["stages"] = [stage for stage in manifest["stages"] if stage["reason_codes"] != ["child_run_started"]
                              or not any(item["stage"] == "github_collection" and item["details"]
                                         for item in manifest["stages"])]
        for target in unique:
            related = {target["normalized_target"], (target["repo_full_name"] or "").casefold()}
            if target["repo_id"]:
                related.update(other["normalized_target"] for other in unique if other["repo_id"] == target["repo_id"])
            bad_stages = [stage for stage in manifest["stages"]
                          if stage["target"] in related and stage["status"] in {"partial", "failed"}]
            if target["repo_id"] and bad_stages:
                target["status"] = "partial"
                target["missing_reasons"] = sorted(set(target["missing_reasons"]) | {
                    reason for stage in bad_stages
                    for reason in (stage["reason_codes"] or [stage["stage"] + "_" + stage["status"]])})
        final_targets = {target["normalized_target"]: target for target in unique}
        manifest["targets"] = [dict(final_targets[target["normalized_target"]], input=target["input"]) for target in parsed]
        bad = manifest["errors"] or any(stage["status"] in {"failed", "partial"} for stage in manifest["stages"])
        resolved = sum(target["repo_id"] is not None for target in manifest["targets"])
        manifest["status"] = ("complete" if not bad and resolved == len(parsed) else
                              "partial" if processing and processing.processing_status != "failed" else "failed")
        manifest["finished_at"] = format_utc_timestamp(self.clock())
        report = dict(report_version="1", integration_run_id=manifest["integration_run_id"], mode=mode,
                      scope=dict(requested_targets=manifest["requested_targets"], child_run_ids=manifest["child_run_ids"],
                                 collection="current_child_runs_only" if mode == "live" else "stored_evidence_only_no_new_collection",
                                 processing="all_eligible_root_raw_inputs", data_profile="storage_root_snapshot_after_run"),
                      started_at=manifest["started_at"], finished_at=manifest["finished_at"],
                      overall_status=manifest["status"], targets=manifest["targets"], sources=manifest["sources"],
                      totals=dict(targets_requested=len(parsed), targets_resolved=resolved,
                          repositories_excluded=sum(entry["excluded"] for entry in manifest["sources"]
                                                    if entry["source"] == "github_metadata"),
                          repositories_failed=sum(entry["failed"] for entry in manifest["sources"]
                                                  if entry["source"] == "github_metadata"),
                          repositories_collected=len({raw.repo_id for _, raw in repositories.values()}) if mode == "live" else 0,
                          records_newly_stored=(
                              None if mode == "live" and any(entry["attempted"] and entry["records_stored"] is None
                                                            for entry in manifest["sources"])
                              else sum(entry["records_stored"] or 0 for entry in manifest["sources"]) if mode == "live" else 0),
                          records_newly_stored_known=sum(entry["records_stored"] or 0 for entry in manifest["sources"]),
                          sources_with_unknown_storage_counts=[entry["source"] for entry in manifest["sources"]
                                                               if entry["attempted"] and entry["records_stored"] is None]),
                      source_coverage="not_verified", historical_validity="unverified",
                      errors=manifest["errors"], limitations=manifest["limitations"])
        try:
            write_json_atomic(collection_path, report)
        except EXPECTED_ERRORS as error:
            self._error(manifest, None, "profiling", "collection_report_failed", error)
            manifest["status"] = "failed"
        self._save(manifest)
        return CollectionPipelineResult(
            integration_run_id=manifest["integration_run_id"], mode=mode, status=manifest["status"],
            targets_requested=len(parsed), targets_resolved=resolved,
            repositories_collected=report["totals"]["repositories_collected"], child_run_ids=manifest["child_run_ids"],
            processing_run_id=manifest["processing"].get("processing_run_id"),
            processing_status=manifest["processing"].get("status", "failed"), manifest_path=self._path(manifest),
            collection_report_path=collection_path, data_profile_path=quality_path, errors=manifest["errors"],
            missing_reasons=sorted({reason for target in manifest["targets"] for reason in target["missing_reasons"]}))
