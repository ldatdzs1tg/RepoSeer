"""External discovery pipeline orchestrating multi-query search, deduplication, and raw persistence."""

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from reposeer.clients.external.base import DiscoveryProvider
from reposeer.clients.external.devto import DevToClient
from reposeer.clients.external.hackernews import HackerNewsClient
from reposeer.clients.external.news import NewsAPIProvider
from reposeer.clients.external.rss import RSSProvider
from reposeer.clients.external.stackexchange import StackExchangeClient
from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.collection.progress import CollectionProgress
from reposeer.collection.run_manager import RunManager
from reposeer.collectors.external.discovery.base import ExternalDiscoveryCollector
from reposeer.contracts import (
    CollectionRequest,
    CollectionResult,
    CollectionSubject,
    RawRecord,
    RepoIdentity,
    TimeRange,
)
from reposeer.contracts.identity import format_utc_timestamp
from reposeer.processing.normalize.timestamps import normalize_datetime
from reposeer.services.repository_service import parse_repository_target
from reposeer.storage.paths import StoragePaths, paths
from reposeer.storage.raw.jsonl import JsonlStorage
from reposeer.technology import TechnologyDetector
from reposeer.technology.functional_profile import FunctionalProfileResult
from reposeer.technology.models import DetectionResult, SearchPlan
from reposeer.technology.search_terms import SearchTermGenerator


@dataclass
class ExternalDiscoveryPipelineResult:
    """Result of external discovery pipeline execution."""

    collection_result: CollectionResult
    search_plan: SearchPlan
    discovery_manifest: dict[str, Any]
    output_path: Path
    run_id: str

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)


class ExternalPipeline:
    """Orchestrates T-016 external discovery with cutoff-safety and canonical raw persistence."""

    def __init__(
        self,
        *,
        http_client: HTTPClient | None = None,
        storage_paths: StoragePaths | None = None,
        storage: JsonlStorage | None = None,
        run_manager: RunManager | None = None,
        clock: Callable[[], datetime] | None = None,
        providers: Mapping[str, DiscoveryProvider] | None = None,
    ) -> None:
        self._owns_http = http_client is None
        self.http = http_client if http_client is not None else HTTPClient()
        self.storage_paths = storage_paths or paths
        self.storage = storage or JsonlStorage()
        self.run_manager = (
            run_manager
            if run_manager is not None
            else RunManager(self.storage_paths.metadata_dir / "collection_runs")
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.custom_providers = dict(providers) if providers is not None else None

    def __enter__(self) -> "ExternalPipeline":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close underlying HTTP client if owned."""
        if self._owns_http and self.http is not None:
            self.http.close()

    def _resolve_providers(
        self,
        sources: Sequence[str] | None,
        progress: CollectionProgress | None,
    ) -> dict[str, DiscoveryProvider]:
        """Instantiate or filter discovery providers."""
        if self.custom_providers is not None:
            if sources is None:
                return dict(self.custom_providers)
            src_set = {s.strip().lower() for s in sources}
            return {k: v for k, v in self.custom_providers.items() if k.lower() in src_set}

        src_list = sources or ["hackernews"]
        resolved: dict[str, DiscoveryProvider] = {}
        for s in src_list:
            norm = s.strip().lower()
            if norm == "hackernews":
                resolved["hackernews"] = HackerNewsClient(
                    http_client=self.http, progress=progress, clock=self.clock
                )
            elif norm in ("stackexchange", "stackoverflow"):
                resolved["stackexchange"] = StackExchangeClient(
                    http_client=self.http, progress=progress, clock=self.clock
                )
            elif norm == "devto":
                resolved["devto"] = DevToClient(
                    http_client=self.http, progress=progress, clock=self.clock
                )
            elif norm == "rss":
                resolved["rss"] = RSSProvider(
                    http_client=self.http, progress=progress, clock=self.clock
                )
            elif norm in ("news", "newsapi"):
                resolved["news"] = NewsAPIProvider(
                    http_client=self.http, progress=progress, clock=self.clock
                )
        return resolved

    def discover(
        self,
        target: RawRecord | RepoIdentity | str | None = None,
        *,
        repo_identity: RepoIdentity | None = None,
        package_id: str | None = None,
        detection_result: DetectionResult | None = None,
        functional_profile: FunctionalProfileResult | None = None,
        cutoff_at: str | datetime | None = None,
        time_range: TimeRange | tuple[datetime, datetime] | None = None,
        window_days: int | None = None,
        cursor: str | None = None,
        max_pages: int = 5,
        max_records: int = 100,
        sources: Sequence[str] | None = None,
        run_id: str | None = None,
        historical_mode: bool = False,
    ) -> ExternalDiscoveryPipelineResult:
        """Run external discovery for a repository or package subject."""
        repo_full_name: str | None = None
        repo_id: str | None = None

        if isinstance(target, RawRecord):
            if target.source_record_type == "github_repository":
                repo_full_name = target.payload.get("full_name")  # type: ignore[assignment]
                repo_id = target.repo_id
            elif target.source_url and "github.com/" in target.source_url:
                repo_full_name = target.source_url.split("github.com/")[1].rstrip("/")
            if package_id is None:
                package_id = target.package_id
            if detection_result is None:
                detection_result = TechnologyDetector(clock=self.clock).detect(target)

        elif isinstance(target, RepoIdentity):
            repo_full_name = target.repo_full_name
            repo_id = target.repo_id
            repo_identity = repo_identity or target
            if detection_result is None:
                detection_result = TechnologyDetector(clock=self.clock).detect(repo_full_name)

        elif isinstance(target, str):
            clean_target = target.strip()
            if clean_target.startswith("pypi:"):
                package_id = package_id or clean_target
            else:
                try:
                    repo_full_name = parse_repository_target(clean_target)
                except ValueError:
                    repo_full_name = clean_target
            if detection_result is None:
                detection_result = TechnologyDetector(clock=self.clock).detect(
                    repo_full_name or package_id
                )

        elif repo_identity is not None:
            repo_full_name = repo_identity.repo_full_name
            repo_id = repo_identity.repo_id
            if detection_result is None:
                detection_result = TechnologyDetector(clock=self.clock).detect(repo_full_name)

        elif package_id is not None:
            if detection_result is None:
                detection_result = TechnologyDetector(clock=self.clock).detect(package_id)

        else:
            raise ValueError("Must provide at least one target, repo_identity, or package_id")

        if detection_result is None:
            detection_result = TechnologyDetector(clock=self.clock).detect(
                repo_full_name or package_id
            )

        specs = detection_result.resolved_specs if detection_result else []
        primary_spec = detection_result.primary_spec if detection_result else None
        entities = detection_result.entities if detection_result else []

        search_plan = SearchTermGenerator().build_search_plan(
            specs=specs,
            primary_spec=primary_spec,
            repo_full_name=repo_full_name,
            package_id=package_id,
            repo_id=repo_id,
            functional_profile=functional_profile,
            entities=entities,
        )

        now = self.clock()
        cutoff_at_str: str | None = None
        if isinstance(cutoff_at, datetime):
            cutoff_at_str = format_utc_timestamp(cutoff_at)
        elif isinstance(cutoff_at, str) and cutoff_at.strip():
            cutoff_at_str = cutoff_at.strip()

        req_time_range: TimeRange | None = None
        if isinstance(time_range, TimeRange):
            req_time_range = time_range
        elif isinstance(time_range, tuple) and len(time_range) == 2:
            req_time_range = TimeRange(
                start_at=format_utc_timestamp(time_range[0]),
                end_at=format_utc_timestamp(time_range[1]),
            )
        elif window_days is not None and window_days > 0:
            end_dt = normalize_datetime(cutoff_at_str) if cutoff_at_str else now
            if end_dt is None:
                end_dt = now
            start_dt = end_dt - timedelta(days=window_days)
            req_time_range = TimeRange(
                start_at=format_utc_timestamp(start_dt),
                end_at=format_utc_timestamp(end_dt),
            )

        _uuid4_re = re.compile(
            r"^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$"
        )
        if run_id is not None:
            if _uuid4_re.match(run_id):
                run_id_val = run_id
            else:
                h = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
                run_id_val = f"{h[:8]}-{h[8:12]}-4{h[13:16]}-8{h[17:20]}-{h[20:32]}"
        else:
            run_id_val = str(uuid4())

        context = self.run_manager.resume_run(run_id_val)
        if context is None:
            context = self.run_manager.start_run(run_id_val)
        progress = CollectionProgress(context, self.run_manager)

        providers = self._resolve_providers(sources, progress)
        collector = ExternalDiscoveryCollector(providers, progress=progress, clock=self.clock)

        request = CollectionRequest(
            schema_version="0.1.0",
            record_type="collection_request",
            request_id=str(uuid4()),
            run_id=context.run_id,
            collector_name="external_discovery",
            source="web_article",
            subjects=[
                CollectionSubject(
                    repo_id=repo_id,
                    repo_full_name=repo_full_name,
                    package_id=package_id,
                    url=None,
                )
            ],
            record_kinds=["article"],
            time_range=req_time_range,
            cutoff_at=cutoff_at_str,
            cursor=cursor,
            max_pages=max_pages,
            max_records=max_records,
        )

        collection_result, discovery_manifest = collector.collect_discovery(
            request,
            search_plan,
            repo_identity=repo_identity,
            package_id=package_id,
            historical_mode=historical_mode,
        )

        out_path = self.storage_paths.raw_path("external", "discovery")
        collector.save_discovery_records(collection_result, target_path=out_path, storage=self.storage)

        context.set_checkpoint("query_set_hash", search_plan.query_set_hash)
        context.set_checkpoint("query_version", search_plan.query_version)
        context.set_checkpoint(
            "pagination_complete", 1 if collection_result.metadata.pagination_complete else 0
        )
        if collection_result.metadata.next_cursor:
            context.set_checkpoint("next_cursor", collection_result.metadata.next_cursor)
        self.run_manager.save_checkpoint(context)

        manifest_dir = self.storage_paths.metadata_dir / "collection_runs"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / f"discovery_manifest_{context.run_id}.json"
        write_json_atomic(manifest_path, discovery_manifest)

        return ExternalDiscoveryPipelineResult(
            collection_result=collection_result,
            search_plan=search_plan,
            discovery_manifest=discovery_manifest,
            output_path=out_path,
            run_id=context.run_id,
        )

    def run(self, target: Any, **kwargs: Any) -> ExternalDiscoveryPipelineResult:
        """Run external discovery pipeline."""
        return self.discover(target, **kwargs)
