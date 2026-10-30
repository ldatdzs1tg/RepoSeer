"""External discovery pipeline orchestrating multi-query search, deduplication, and raw persistence."""

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from reposeer.clients.external.base import DiscoveryProvider, is_historical_discovery
from reposeer.clients.external.devto import DevToClient
from reposeer.clients.external.hackernews import HackerNewsClient
from reposeer.clients.external.news import NewsAPIProvider
from reposeer.clients.external.rss import RSSProvider
from reposeer.clients.external.stackexchange import StackExchangeClient
from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.collection.progress import CollectionProgress
from reposeer.collection.run_manager import RunManager
from reposeer.collectors.external.discovery.base import (
    ExternalDiscoveryCollector,
    input_observation_scope,
    load_discovery_state,
)
from reposeer.config import settings
from reposeer.contracts import (
    CollectionRequest,
    CollectionResult,
    CollectionSubject,
    RawRecord,
    RepoIdentity,
    TimeRange,
)
from reposeer.contracts.identity import canonical_github_url, format_utc_timestamp
from reposeer.contracts.identity import package_id as canonical_package_id
from reposeer.contracts.identity import repo_id as canonical_repo_id
from reposeer.contracts.validation import validate_record
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
        aliases = {"stackoverflow": "stackexchange", "newsapi": "news"}
        requested = None if sources is None else {aliases.get(name.strip().lower(), name.strip().lower()) for name in sources}
        if self.custom_providers is not None:
            configured = {aliases.get(name.lower(), name.lower()): provider for name, provider in self.custom_providers.items()}
            if requested is not None and requested - configured.keys():
                raise ValueError("Unsupported or unconfigured discovery providers: " + ", ".join(sorted(requested - configured.keys())))
            return {name: provider for name, provider in configured.items() if requested is None or name in requested}
        factories = {"hackernews": HackerNewsClient, "stackexchange": StackExchangeClient, "devto": DevToClient}
        api_keys = {"devto": settings.devto_api_key, "stackexchange": settings.stackexchange_api_key}
        requested = {"hackernews"} if requested is None else requested
        if requested - (factories.keys() | {"rss", "news"}):
            raise ValueError("Unsupported discovery provider; supported: hackernews, devto, stackexchange, rss, news")
        resolved: dict[str, DiscoveryProvider] = {}
        for name in sorted(requested):
            if name == "rss":
                resolved[name] = RSSProvider([url.strip() for url in settings.rss_feeds.split(",") if url.strip()],
                                             http_client=self.http, clock=self.clock)
            elif name == "news":
                resolved[name] = NewsAPIProvider(settings.news_api_key, http_client=self.http, clock=self.clock)
            else:
                # Provider fetch checkpoints are not durable discovery job checkpoints.
                credentials = {"api_key": api_keys[name] or None} if name in api_keys else {}
                resolved[name] = factories[name](http_client=self.http, clock=self.clock, **credentials)
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
        resume: bool = False,
        include_technology_context: bool = False,
        extended_package_queries: bool = False,
    ) -> ExternalDiscoveryPipelineResult:
        """Run external discovery for a repository or package subject."""
        repo_full_name: str | None = None
        repo_id: str | None = None

        if repo_identity is not None:
            repo_identity = RepoIdentity.model_validate(repo_identity.model_dump())
        if isinstance(target, RawRecord):
            target = validate_record(target)
            if target.source not in {"github_rest", "github_graphql"} or target.source_record_type != "github_repository":
                raise ValueError("Discovery requires a GitHub repository observation")
            native_id = target.payload.get("id")
            repo_full_name = target.payload.get("full_name")
            if type(native_id) is not int or native_id <= 0 or not isinstance(repo_full_name, str):
                raise ValueError("GitHub observation requires native numeric id and full_name")
            if target.repo_id != canonical_repo_id(str(native_id)):
                raise ValueError("GitHub native id and canonical repo_id mismatch")
            if target.source_record_id is not None and target.source_record_id != str(native_id):
                raise ValueError("GitHub native id and source_record_id mismatch")
            raw_identity = RepoIdentity(repo_id=target.repo_id, github_repo_id=str(native_id),
                                        github_node_id=None, repo_full_name=repo_full_name,
                                        repo_url=canonical_github_url(repo_full_name), known_urls=[])
            if repo_identity is not None and (repo_identity.repo_id != raw_identity.repo_id
                                             or repo_identity.repo_full_name.casefold() != repo_full_name.casefold()):
                raise ValueError("Supplied repository identity and GitHub observation mismatch")
            repo_identity = repo_identity or raw_identity
            package_id = package_id or target.package_id
            if detection_result is None:
                detection_result = TechnologyDetector(clock=self.clock).detect(target)
        elif isinstance(target, RepoIdentity):
            target = RepoIdentity.model_validate(target.model_dump())
            if repo_identity is not None and repo_identity != target:
                raise ValueError("Conflicting repository identities")
            repo_identity = target
        elif isinstance(target, str):
            clean = target.strip()
            if clean.startswith("pypi:"):
                named_package = canonical_package_id(clean.removeprefix("pypi:"))
                if package_id is not None and package_id != named_package:
                    raise ValueError("Conflicting package targets")
                package_id = named_package
            else:
                repo_full_name = parse_repository_target(clean)
                if repo_identity is not None and repo_full_name.casefold() != repo_identity.repo_full_name.casefold():
                    raise ValueError("Manual target and verified repository identity mismatch")
        elif target is not None:
            raise TypeError("target must be a GitHub RawRecord, RepoIdentity, repository name, or pypi:name")
        if repo_identity is not None:
            repo_full_name, repo_id = repo_identity.repo_full_name, repo_identity.repo_id
        if package_id is not None:
            if not package_id.startswith("pypi:") or canonical_package_id(package_id[5:]) != package_id:
                raise ValueError("package_id must be a canonical pypi identifier")
        if not repo_full_name and not package_id:
            raise ValueError("Must provide a repository or package target")

        if detection_result is None:
            detection_result = TechnologyDetector(clock=self.clock).detect(
                repo_full_name or package_id
            )

        specs = detection_result.resolved_specs if detection_result else []
        primary_spec = detection_result.primary_spec if detection_result else None
        entities = detection_result.entities if detection_result else []

        now = self.clock()
        cutoff_at_str: str | None = None
        if isinstance(cutoff_at, datetime):
            cutoff_at_str = format_utc_timestamp(cutoff_at)
        elif isinstance(cutoff_at, str) and cutoff_at.strip():
            cutoff_at_str = cutoff_at.strip()

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

        if resume and run_id is None:
            raise ValueError("resume requires the saved run_id")
        context = self.run_manager.resume_run(run_id_val)
        state_path = self.run_manager.checkpoints_dir / f"discovery_state_{run_id_val}.json"
        saved_scope = load_discovery_state(state_path)["scope"] if state_path.exists() else None
        if context is not None and saved_scope is None:
            raise ValueError("Existing run has no durable discovery job state; start a new run instead of guessing a legacy cursor")
        if resume and (context is None or saved_scope is None):
            raise ValueError("No durable discovery state exists for this run_id")
        input_observation = target if isinstance(target, RawRecord) else None
        if saved_scope is not None:
            original_input = {key: saved_scope.get(key) for key in input_observation_scope(None)}
            if original_input != input_observation_scope(input_observation):
                raise ValueError("Original upstream observation identity or integrity differs from saved run")
            if saved_scope.get("include_technology_context") != include_technology_context:
                raise ValueError("Resume technology-context mode differs from saved run; repeat the original option")
        if saved_scope is not None and cutoff_at is None:
            cutoff_at_str = saved_scope["cutoff_at"]
        req_time_range: TimeRange | None = None
        if isinstance(time_range, TimeRange):
            req_time_range = TimeRange.model_validate(time_range.model_dump())
        elif isinstance(time_range, tuple) and len(time_range) == 2:
            req_time_range = TimeRange(start_at=format_utc_timestamp(time_range[0]), end_at=format_utc_timestamp(time_range[1]))
        elif saved_scope is not None and saved_scope["window"]["start_at"] is not None:
            req_time_range = TimeRange(**saved_scope["window"])
            if window_days is not None and (normalize_datetime(req_time_range.end_at) - normalize_datetime(req_time_range.start_at)) != timedelta(days=window_days):
                raise ValueError("Resume window_days differs from saved window")
        elif window_days is not None:
            if type(window_days) is not int or window_days <= 0:
                raise ValueError("window_days must be positive")
            end_dt = normalize_datetime(cutoff_at_str) if cutoff_at_str else now
            req_time_range = TimeRange(start_at=format_utc_timestamp(end_dt - timedelta(days=window_days)),
                                      end_at=format_utc_timestamp(end_dt))
        historical = is_historical_discovery(cutoff_at=normalize_datetime(cutoff_at_str) if cutoff_at_str else None,
                                              window_end=normalize_datetime(req_time_range.end_at) if req_time_range else None,
                                              now=now, explicit=historical_mode)
        if saved_scope is not None and historical_mode is False:
            # Temporal mode is part of the immutable execution scope on resume.
            historical = saved_scope["historical_mode"]
        search_plan = SearchTermGenerator().build_search_plan(
            specs=specs, primary_spec=primary_spec, repo_full_name=repo_full_name,
            package_id=package_id, repo_id=repo_id, functional_profile=functional_profile,
            entities=entities, cutoff_at=cutoff_at_str, historical_mode=historical,
            include_technology_context=include_technology_context,
            extended_package_queries=extended_package_queries,
            query_version="2.0.0" if extended_package_queries else "1.0.0")
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
            historical_mode=historical,
            input_observation=input_observation,
        )

        discovery_manifest["identity_verification"] = {
            "repository": "validated_upstream" if repo_identity is not None else "unverified" if repo_full_name else "not_provided",
            "package_repository_mapping": "not_verified_by_discovery",
        }
        out_path = self.storage_paths.raw_path("external", "discovery")
        collector.save_discovery_records(collection_result, target_path=out_path, storage=self.storage)

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
