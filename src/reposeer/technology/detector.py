"""Core technology entity detection and search terms synthesis module."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from reposeer.contracts import GitHubRepository, TechnologyEntity, validate_record
from reposeer.contracts.identity import format_utc_timestamp
from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.technology.agent import LightweightTechAgent
from reposeer.technology.manifest_parser import parse_manifest_dependencies
from reposeer.technology.models import DetectedSignal, DetectionResult, SearchTerm
from reposeer.technology.registry import TechSpec, TechnologyRegistry
from reposeer.technology.search_terms import SearchTermGenerator


def _default_clock() -> datetime:
    return datetime.now(UTC)


class TechnologyDetector:
    """Multi-signal technology detector and search terms generator under T-011 contract."""

    def __init__(
        self,
        registry: TechnologyRegistry | None = None,
        agent: LightweightTechAgent | None = None,
        search_term_generator: SearchTermGenerator | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.registry = registry or TechnologyRegistry()
        self.agent = agent or LightweightTechAgent(self.registry)
        self.search_generator = search_term_generator or SearchTermGenerator()
        self.clock = clock or _default_clock

    def detect(
        self,
        repo: GitHubRepository | RepositoryRecord | dict[str, Any] | str | None = None,
        *,
        readme_content: str | None = None,
        manifest_files: dict[str, str] | None = None,
        topics: list[str] | None = None,
        dependencies: list[str] | None = None,
        pinned_signals: list[str | TechnologyEntity] | None = None,
        retrieved_at: str | None = None,
        run_id: str | None = None,
        raw_record_id: str | None = None,
        source_url: str | None = None,
    ) -> DetectionResult:
        """Detect technology entities and synthesize external search terms."""
        now_ts = retrieved_at or format_utc_timestamp(self.clock())
        raw_id = raw_record_id or f"raw:{uuid4()}"

        # 1. Extract repository metadata
        meta = self._extract_metadata(repo)
        repo_full_name = meta.get("full_name")
        repo_numeric_id = meta.get("numeric_id")
        repo_language = meta.get("language")
        repo_description = meta.get("description")
        all_topics = list(meta.get("topics") or [])
        if topics:
            all_topics.extend(topics)
        all_topics = list(dict.fromkeys(all_topics))

        effective_source_url = source_url or (f"https://github.com/{repo_full_name}" if repo_full_name else "https://github.com/")

        signals: list[DetectedSignal] = []
        specs_by_slug: dict[str, TechSpec] = {}
        sources_by_slug: dict[str, set[str]] = {}
        confidence_by_slug: dict[str, float] = {}

        def record_match(spec: TechSpec, source: str, conf: float, raw_token: str, context: str | None = None):
            specs_by_slug[spec.slug] = spec
            sources_by_slug.setdefault(spec.slug, set()).add(source)
            confidence_by_slug[spec.slug] = max(confidence_by_slug.get(spec.slug, 0.0), conf)
            signals.append(
                DetectedSignal(
                    source=source,  # type: ignore[arg-type]
                    raw_token=raw_token,
                    confidence=conf,
                    context=context,
                )
            )

        # 2. Primary Repository Identity Signal
        primary_slug: str | None = None
        if repo_full_name:
            repo_match = self.registry.lookup_by_repo(repo_full_name)
            if repo_match:
                primary_slug = repo_match.slug
                record_match(
                    repo_match,
                    source="repo_identity",
                    conf=1.0,
                    raw_token=repo_full_name,
                    context=f"Exact repository slug match for {repo_full_name}",
                )

        # 3. Programming Language Signal
        if repo_language:
            lang_match = self.registry.lookup(repo_language)
            if lang_match:
                record_match(
                    lang_match,
                    source="language",
                    conf=0.95,
                    raw_token=repo_language,
                    context=f"Repository primary language: {repo_language}",
                )

        # 4. Topics Signal
        for topic in all_topics:
            topic_match = self.registry.lookup_by_topic(topic)
            if topic_match:
                record_match(
                    topic_match,
                    source="topic",
                    conf=0.90,
                    raw_token=topic,
                    context=f"GitHub topic tag: {topic}",
                )

        # 5. Dependency Manifests Signal
        all_deps = list(dependencies or [])
        if manifest_files:
            all_deps.extend(parse_manifest_dependencies(manifest_files))
        all_deps = list(dict.fromkeys(all_deps))

        for dep_name in all_deps:
            dep_match = self.registry.lookup_by_package(dep_name)
            if dep_match:
                record_match(
                    dep_match,
                    source="dependency",
                    conf=0.85,
                    raw_token=dep_name,
                    context=f"Declared dependency: {dep_name}",
                )

        # 6. Text Parsing & Agent Signal (README and Description)
        combined_text = ""
        if repo_description:
            combined_text += f"{repo_description}\n\n"
        if readme_content:
            combined_text += readme_content

        if combined_text:
            repo_name_only = repo_full_name.split("/")[-1] if repo_full_name and "/" in repo_full_name else None
            agent_matches = self.agent.analyze_text(combined_text, repo_name=repo_name_only)
            for am in agent_matches:
                record_match(
                    am.spec,
                    source="agent" if am.signal_kind in {"title", "install_code", "import_code"} else "readme",
                    conf=am.confidence,
                    raw_token=am.matched_text,
                    context=f"[{am.signal_kind}] {am.context_snippet}",
                )

        # 7. Pinned / Pre-extracted Seed Signals
        if pinned_signals:
            for pinned in pinned_signals:
                if isinstance(pinned, TechnologyEntity):
                    # Direct T-011 entity passed in
                    specs_by_slug[pinned.slug] = TechSpec(
                        slug=pinned.slug,
                        name=pinned.name,
                        category=pinned.category,  # type: ignore[arg-type]
                        aliases=pinned.aliases,
                        description=pinned.description,
                        official_urls=pinned.official_urls,
                        lifecycle_status=pinned.lifecycle_status,  # type: ignore[arg-type]
                        status_evidence_url=pinned.status_evidence_url,
                    )
                    sources_by_slug.setdefault(pinned.slug, set()).add("pinned")
                    confidence_by_slug[pinned.slug] = 1.0
                    signals.append(
                        DetectedSignal(
                            source="pinned",
                            raw_token=pinned.name,
                            confidence=1.0,
                            context=f"Explicitly pinned technology entity: {pinned.technology_id}",
                        )
                    )
                    if not primary_slug:
                        primary_slug = pinned.slug
                elif isinstance(pinned, str):
                    p_match = self.registry.lookup(pinned)
                    if p_match:
                        record_match(
                            p_match,
                            source="pinned",
                            conf=1.0,
                            raw_token=pinned,
                            context=f"Explicitly pinned signal: {pinned}",
                        )
                        if not primary_slug:
                            primary_slug = p_match.slug

        # 8. Instantiate canonical TechnologyEntity instances under T-011
        repo_numeric_ids = [repo_numeric_id] if repo_numeric_id else []
        entities: list[TechnologyEntity] = []

        # Sort entities by confidence descending, then primary first
        sorted_slugs = sorted(
            specs_by_slug.keys(),
            key=lambda s: (s == primary_slug, confidence_by_slug.get(s, 0.0)),
            reverse=True,
        )

        for slug in sorted_slugs:
            spec = specs_by_slug[slug]
            # Attach repository ID to primary technology and dependencies
            entity_repo_ids = repo_numeric_ids if (slug == primary_slug or "repo_identity" in sources_by_slug.get(slug, set())) else []
            contract_entity = spec.to_contract_entity(
                repo_numeric_ids=entity_repo_ids,
                retrieved_at=now_ts,
                source_url=effective_source_url,
                raw_record_id=raw_id,
            )
            entities.append(contract_entity)

        primary_entity = next((e for e in entities if e.slug == primary_slug), (entities[0] if entities else None))

        # 9. Generate prioritized search terms for external information
        search_terms = self.search_generator.generate(
            entities=entities,
            repo_full_name=repo_full_name,
            primary_slug=primary_slug,
            topics=all_topics,
        )

        return DetectionResult(
            entities=entities,
            search_terms=search_terms,
            primary_technology=primary_entity,
            confidence_by_slug=confidence_by_slug,
            detection_sources_by_slug={k: sorted(v) for k, v in sources_by_slug.items()},
            detected_signals=signals,
            metadata={
                "retrieved_at": now_ts,
                "raw_record_id": raw_id,
                "run_id": run_id,
                "repo_full_name": repo_full_name,
                "total_entities_detected": len(entities),
                "total_search_terms_generated": len(search_terms),
                "dependencies_analyzed": len(all_deps),
                "topics_analyzed": len(all_topics),
            },
        )

    def _extract_metadata(self, repo: Any) -> dict[str, Any]:
        """Extract uniform repository metadata from various input types."""
        if repo is None:
            return {}

        if isinstance(repo, GitHubRepository):
            # Extract numeric id from repo_id e.g. "github:repository:123"
            numeric_id = repo.identity.repo_id.split(":")[-1] if repo.identity.repo_id else None
            return {
                "full_name": repo.identity.repo_full_name,
                "numeric_id": numeric_id,
                "language": repo.primary_language,
                "description": repo.description,
                "topics": [],
            }

        if isinstance(repo, RepositoryRecord):
            return {
                "full_name": repo.full_name,
                "numeric_id": str(repo.id) if repo.id else None,
                "language": repo.language,
                "description": repo.description,
                "topics": repo.topics or [],
            }

        if isinstance(repo, dict):
            numeric_id = str(repo.get("id")) if repo.get("id") else None
            return {
                "full_name": repo.get("full_name") or repo.get("repo_full_name"),
                "numeric_id": numeric_id,
                "language": repo.get("language") or repo.get("primary_language"),
                "description": repo.get("description"),
                "topics": repo.get("topics") or [],
            }

        if isinstance(repo, str):
            clean_str = repo.strip()
            return {
                "full_name": clean_str if "/" in clean_str else None,
                "numeric_id": None,
                "language": None,
                "description": None,
                "topics": [],
            }

        return {}


def detect_technologies(
    repo: GitHubRepository | RepositoryRecord | dict[str, Any] | str | None = None,
    *,
    readme_content: str | None = None,
    manifest_files: dict[str, str] | None = None,
    topics: list[str] | None = None,
    dependencies: list[str] | None = None,
    pinned_signals: list[str | TechnologyEntity] | None = None,
    retrieved_at: str | None = None,
    raw_record_id: str | None = None,
    source_url: str | None = None,
) -> DetectionResult:
    """Convenience top-level function to detect technologies and generate search terms."""
    detector = TechnologyDetector()
    return detector.detect(
        repo=repo,
        readme_content=readme_content,
        manifest_files=manifest_files,
        topics=topics,
        dependencies=dependencies,
        pinned_signals=pinned_signals,
        retrieved_at=retrieved_at,
        raw_record_id=raw_record_id,
        source_url=source_url,
    )
