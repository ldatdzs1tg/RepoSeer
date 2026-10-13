"""Deterministic technology resolution with evidence-bound canonical output."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from reposeer.contracts import (
    GitHubRepository,
    Provenance,
    RawRecord,
    TechnologyEntity,
    validate_record,
)
from reposeer.contracts.identity import canonical_github_url, parse_repo_id, repo_id
from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.technology.agent import LightweightTechAgent
from reposeer.technology.manifest_parser import ManifestDependency, parse_manifests
from reposeer.technology.models import DetectedSignal, DetectionResult
from reposeer.technology.registry import TechnologyRegistry, TechSpec, _raw_repository_identity
from reposeer.technology.search_terms import SearchTermGenerator

SOURCE_RANK = {"pinned": 0, "repo_identity": 1, "dependency": 2, "topic": 3,
               "language": 4, "agent": 5, "readme": 5}


def _raw_provenance(raw: RawRecord) -> Provenance:
    return Provenance(
        source=raw.source, source_url=raw.source_url, retrieved_at=raw.retrieved_at,
        valid_at=raw.retrieved_at, available_at=raw.available_at, temporal_basis="observation",
        raw_record_ids=[raw.raw_record_id], missing_fields=[],
    )


def _entity_spec(entity: TechnologyEntity) -> TechSpec:
    return TechSpec(
        slug=entity.slug, name=entity.name, category=entity.category,
        aliases=entity.aliases, description=entity.description, official_urls=entity.official_urls,
        lifecycle_status=entity.lifecycle_status, status_evidence_url=entity.status_evidence_url,
    )


class TechnologyDetector:
    """Resolve catalog concepts first; emit T-011 entities only with recorded evidence."""

    def __init__(
        self, registry: TechnologyRegistry | None = None,
        agent: LightweightTechAgent | None = None,
        search_term_generator: SearchTermGenerator | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.registry = registry or TechnologyRegistry()
        self.agent = agent or LightweightTechAgent(self.registry)
        self.search_generator = search_term_generator or SearchTermGenerator()
        self.clock = clock  # Compatibility only: normalization never substitutes the clock for retrieval.

    def detect(
        self, repo: GitHubRepository | RepositoryRecord | RawRecord | dict[str, Any] | str | None = None,
        *, readme_content: str | None = None, manifest_files: dict[str, str] | None = None,
        topics: list[str] | None = None, dependencies: list[str] | None = None,
        pinned_signals: list[str | TechnologyEntity] | None = None,
        retrieved_at: str | None = None, run_id: str | None = None,
        raw_record_id: str | None = None, source_url: str | None = None,
        raw_records: list[RawRecord] | None = None,
    ) -> DetectionResult:
        """raw_records are caller-recorded observations, not evidence for unrelated manual inputs.

        Repository payloads prove metadata only. source_archive/project_docs payloads may record
        exact readme_content, manifest_files or dependencies for the same verified repo_id.
        A technology_catalog observation proves only its own slug/id.
        """
        diagnostics: list[str] = []
        recorded = [validate_record(raw) for raw in (raw_records or [])]
        if not all(isinstance(raw, RawRecord) for raw in recorded):
            raise TypeError("raw_records must contain canonical RawRecord objects")
        if isinstance(repo, RawRecord):
            repo = validate_record(repo)
            if repo.source_record_type != "github_repository":
                raise ValueError("repo RawRecord must describe a GitHub repository")
            recorded.append(repo)
        by_raw_id = {}
        for raw in recorded:
            if raw.raw_record_id in by_raw_id and by_raw_id[raw.raw_record_id] != raw:
                raise ValueError("Conflicting observations share a raw_record_id")
            by_raw_id[raw.raw_record_id] = raw
        recorded = [by_raw_id[key] for key in sorted(by_raw_id)]
        if isinstance(repo, GitHubRepository):
            repo = validate_record(repo)
        meta = self._extract_metadata(repo)
        full_name = meta.get("full_name")
        if full_name:
            try:
                canonical_github_url(full_name)
            except ValueError:
                diagnostics.append("Invalid repository subject; retained as unverified input")
                full_name = None
        numeric_id = meta.get("numeric_id")
        supplied_repo_id = None
        invalid_supplied_id = meta.get("invalid_numeric_id", False)
        if numeric_id is not None:
            try:
                supplied_repo_id = repo_id(numeric_id)
            except ValueError:
                invalid_supplied_id = True
        if invalid_supplied_id:
            diagnostics.append("Invalid GitHub numeric ID; no canonical repository ID emitted")
        verified_repo_id = repo.identity.repo_id if isinstance(repo, GitHubRepository) else None
        repo_provenance = repo.provenance if isinstance(repo, GitHubRepository) else None
        metadata_observations: list[tuple[dict[str, Any], Provenance]] = []
        if repo_provenance is not None:
            metadata_observations.append((meta, repo_provenance))
        names_by_id: dict[str, str] = {}
        ids_by_name: dict[str, str] = {}
        if verified_repo_id:
            names_by_id[verified_repo_id] = full_name.casefold()
            ids_by_name[full_name.casefold()] = verified_repo_id
        for raw in recorded:
            if raw.source_record_type != "github_repository":
                continue
            observed_id, observed_name = _raw_repository_identity(raw)
            name_key = observed_name.casefold()
            if (observed_id in names_by_id and names_by_id[observed_id] != name_key
                    or name_key in ids_by_name and ids_by_name[name_key] != observed_id):
                raise ValueError("Conflicting recorded repository identities")
            names_by_id[observed_id] = name_key
            ids_by_name[name_key] = observed_id
            matches_name = bool(full_name and name_key == full_name.casefold())
            matches_id = supplied_repo_id is not None and supplied_repo_id == observed_id
            if not matches_name and not matches_id:
                continue  # Unrelated observations cannot verify this supplied subject.
            if ((full_name and not matches_name) or (supplied_repo_id and not matches_id)):
                raise ValueError("Recorded repository identity disagrees with supplied subject")
            if invalid_supplied_id or not full_name:
                continue
            verified_repo_id = observed_id
            observed_provenance = _raw_provenance(raw)
            if repo_provenance is None:
                repo_provenance = observed_provenance
            metadata_observations.append((self._extract_metadata(raw.payload), observed_provenance))
        if supplied_repo_id and verified_repo_id is None:
            diagnostics.append("Supplied repository ID is unverified; matching repository evidence missing")

        def metadata_evidence(key: str, value: Any) -> Provenance | None:
            for observation, provenance in metadata_observations:
                if key == "topics":
                    if value in (observation.get(key) or []):
                        return provenance
                elif observation.get(key) == value:
                    return provenance
            return None

        if repo_provenance and (
            (retrieved_at is not None and retrieved_at != repo_provenance.retrieved_at)
            or (source_url is not None and source_url != repo_provenance.source_url)
        ):
            diagnostics.append("Supplied observation overrides ignored; recorded source/time preserved")
        subject_url = canonical_github_url(full_name) if full_name else None
        signals: list[DetectedSignal] = []
        specs: dict[str, TechSpec] = {}
        ranks: dict[str, tuple[int, float]] = {}
        sources: dict[str, set[str]] = {}
        evidence: dict[str, tuple[int, Provenance]] = {}
        pinned_entities: dict[str, TechnologyEntity] = {}

        def content_evidence(key: str, value: Any) -> Provenance | None:
            for raw in recorded:
                if raw.source not in {"source_archive", "project_docs"}:
                    continue
                if not verified_repo_id or raw.repo_id != verified_repo_id:
                    continue
                observed_name = raw.payload.get("full_name")
                if observed_name is not None and (not isinstance(observed_name, str) or not full_name
                                                  or observed_name.casefold() != full_name.casefold()):
                    continue
                if key in raw.payload and raw.payload[key] == value:
                    return _raw_provenance(raw)
                if value is not None and key in raw.payload:
                    diagnostics.append(f"{key}: supplied content differs from recorded evidence")
            return None

        def record(spec, source, confidence, token, context, relationship, provenance=None, scope=None):
            specs.setdefault(spec.slug, spec)
            sources.setdefault(spec.slug, set()).add(source)
            rank = (SOURCE_RANK[source], -confidence)
            ranks[spec.slug] = min(ranks.get(spec.slug, rank), rank)
            if provenance is not None:
                current = evidence.get(spec.slug)
                if current is None or SOURCE_RANK[source] < current[0]:
                    evidence[spec.slug] = (SOURCE_RANK[source], provenance)
            signals.append(DetectedSignal(
                source=source, raw_token=token, confidence=confidence, context=context,
                technology_id=f"tech:{spec.slug}", repo_id=verified_repo_id,
                repo_full_name=full_name, relationship=relationship, dependency_scope=scope,
                source_url=provenance.source_url if provenance else (source_url or subject_url),
                raw_record_ids=list(provenance.raw_record_ids) if provenance else [],
                observation_source=provenance.source if provenance else None,
                retrieved_at=provenance.retrieved_at if provenance else None,
                available_at=provenance.available_at if provenance else None,
            ))

        pinned_slugs = []
        for pinned in pinned_signals or []:
            if isinstance(pinned, TechnologyEntity):
                entity = validate_record(pinned)
                spec = self.registry.get_by_slug(entity.slug) or _entity_spec(entity)
                if entity.slug in pinned_entities and pinned_entities[entity.slug] != entity:
                    raise ValueError("Conflicting pinned entities share a technology slug")
                pinned_entities[entity.slug] = entity
                record(spec, "pinned", 1.0, entity.technology_id, "Validated pinned entity", "pinned", entity.provenance)
            else:
                spec = self.registry.lookup(pinned)
                if spec is None:
                    diagnostics.append(f"Unknown or malformed pinned technology: {pinned}")
                    continue
                record(spec, "pinned", 1.0, pinned, "Manually pinned catalog identity", "pinned")
            pinned_slugs.append(spec.slug)
        upstream = self.registry.lookup_by_repo(full_name) if full_name else None
        if upstream:
            record(upstream, "repo_identity", 1.0, full_name, "Curated exact upstream mapping", "implements", repo_provenance)
        primary_slug = min(pinned_slugs) if pinned_slugs else (upstream.slug if upstream else None)

        parsed = parse_manifests(manifest_files or {})
        diagnostics.extend(parsed.diagnostics)
        dependency_items = list(parsed.dependencies)
        dependency_items.extend(ManifestDependency(dep, "manual", "runtime", dep) for dep in (dependencies or []))
        for dep in dependency_items:
            spec = self.registry.lookup_by_package(dep.name)
            if spec is None:
                diagnostics.append(f"Unrecognized dependency: {dep.name} ({dep.filename})")
                continue
            if dep.filename == "manual":
                proven = content_evidence("dependencies", dependencies)
            else:
                proven = content_evidence("manifest_files", manifest_files)
            relationship = "depends_on"
            if dep.scope.startswith("optional:"):
                relationship = "optional_dependency"
            elif dep.scope.startswith("dev:"):
                relationship = "dev_dependency"
            record(spec, "dependency", 0.90, dep.name, f"{dep.filename}: {dep.declaration}", relationship, proven, dep.scope)

        metadata_topics = set(meta.get("topics") or [])
        all_topics = sorted(metadata_topics | set(topics or []), key=lambda value: (value.casefold(), value))
        for topic in all_topics:
            spec = self.registry.lookup_by_topic(topic)
            if spec:
                record(spec, "topic", 0.85, topic, "Explicit topic", "topic", metadata_evidence("topics", topic))
        language = meta.get("language")
        if language:
            spec = self.registry.lookup(language)
            if spec and spec.category == "language":
                record(spec, "language", 0.80, language, "GitHub primary language", "language", metadata_evidence("language", language))
        for text, provenance, label in (
            (meta.get("description"), metadata_evidence("description", meta.get("description")), "description"),
            (readme_content, content_evidence("readme_content", readme_content), "README"),
        ):
            if not text:
                continue
            for match in self.agent.analyze_text(text):
                source = "agent" if match.signal_kind in {"title", "install_code", "import_code"} else "readme"
                record(match.spec, source, match.confidence, match.matched_text,
                       f"{label} [{match.signal_kind}] {match.context_snippet}", "mentions", provenance)

        sorted_slugs = sorted(specs, key=lambda slug: (slug != primary_slug, ranks[slug], slug))
        entities: list[TechnologyEntity] = []
        for slug in sorted_slugs:
            if slug in pinned_entities:
                entities.append(pinned_entities[slug])
                continue
            proven = evidence.get(slug)
            if proven is None:
                for raw in recorded:
                    if raw.source != "technology_catalog":
                        continue
                    try:
                        specs[slug]._validate_catalog_observation(raw)
                    except ValueError as error:
                        if any(raw.payload.get(key) in (slug, f"tech:{slug}")
                               for key in ("slug", "id", "technology_id")):
                            diagnostics.append(f"{slug}: catalog evidence rejected: {error}")
                        continue
                    proven = (0, _raw_provenance(raw))
                    break
            if proven is None:
                diagnostics.append(f"{slug}: canonical entity omitted; recorded evidence missing")
                continue
            linked_repos = []
            if verified_repo_id and any(s.technology_id == f"tech:{slug}" and s.relationship == "implements"
                                         and s.raw_record_ids for s in signals):
                linked_repos = [parse_repo_id(verified_repo_id)]
            try:
                entities.append(specs[slug]._from_verified_provenance(proven[1], repo_numeric_ids=linked_repos))
            except ValueError as error:
                diagnostics.append(f"{slug}: canonical entity omitted: {error}")
        if raw_record_id is not None and not any(raw_record_id in e.provenance.raw_record_ids for e in entities):
            diagnostics.append("Supplied raw_record_id alone is insufficient evidence")
        resolved = [specs[slug] for slug in sorted_slugs]
        terms = self.search_generator.generate(
            entities=resolved, repo_full_name=full_name, primary_slug=primary_slug,
            topics=all_topics, verified_subject=verified_repo_id is not None,
        )
        # Deduplicate complete evidence records, keeping conflicting semantics distinct.
        unique_signals = {signal.model_dump_json(): signal for signal in signals}
        ordered_signals = sorted(unique_signals.values(), key=lambda s: (
            s.technology_id or "", SOURCE_RANK[s.source], s.relationship,
            s.raw_token.casefold(), s.context or "", s.source_url or "", tuple(s.raw_record_ids),
        ))
        primary_entity = next((e for e in entities if e.slug == primary_slug), None)
        return DetectionResult(
            resolved_specs=resolved, primary_spec=specs.get(primary_slug),
            entities=entities, primary_technology=primary_entity, search_terms=terms,
            confidence_by_slug={slug: -ranks[slug][1] for slug in sorted_slugs},
            detection_sources_by_slug={slug: sorted(sources[slug]) for slug in sorted_slugs},
            detected_signals=ordered_signals, diagnostics=sorted(set(diagnostics)),
            metadata={"input_mode": "recorded" if repo_provenance or recorded else "manual",
                      "retrieved_at": repo_provenance.retrieved_at if repo_provenance else None,
                      "run_id": run_id, "repo_full_name": full_name, "repo_id": verified_repo_id,
                      "supplied_repo_id": supplied_repo_id,
                      "total_entities_detected": len(entities), "total_resolved_specs": len(resolved),
                      "total_search_terms_generated": len(terms),
                      "dependencies_analyzed": len(dependency_items), "topics_analyzed": len(all_topics)},
        )

    @staticmethod
    def _extract_metadata(repo: Any) -> dict[str, Any]:
        if isinstance(repo, GitHubRepository):
            repo = validate_record(repo)
            return {"full_name": repo.identity.repo_full_name, "numeric_id": repo.identity.github_repo_id,
                    "language": repo.primary_language, "description": repo.description}
        if isinstance(repo, RepositoryRecord):
            return {"full_name": repo.full_name, "numeric_id": str(repo.id) if type(repo.id) is int else None,
                    "language": repo.language, "description": repo.description, "topics": repo.topics,
                    "invalid_numeric_id": repo.id is not None and type(repo.id) is not int}
        if isinstance(repo, RawRecord):
            _raw_repository_identity(repo)
            return TechnologyDetector._extract_metadata(repo.payload)
        if isinstance(repo, dict):
            numeric = repo.get("id")
            return {"full_name": repo.get("full_name") or repo.get("repo_full_name"),
                    "numeric_id": str(numeric) if type(numeric) in {int, str} else None,
                    "invalid_numeric_id": numeric is not None and type(numeric) not in {int, str},
                    "language": repo.get("language") or repo.get("primary_language"),
                    "description": repo.get("description"), "topics": repo.get("topics") or [], "payload": repo}
        if isinstance(repo, str):
            return {"full_name": repo.strip()}
        return {}


def detect_technologies(
    repo: GitHubRepository | RepositoryRecord | RawRecord | dict[str, Any] | str | None = None,
    *, readme_content: str | None = None, manifest_files: dict[str, str] | None = None,
    topics: list[str] | None = None, dependencies: list[str] | None = None,
    pinned_signals: list[str | TechnologyEntity] | None = None,
    retrieved_at: str | None = None, raw_record_id: str | None = None,
    source_url: str | None = None, raw_records: list[RawRecord] | None = None,
) -> DetectionResult:
    """Resolve manually or from caller-recorded observations, without network/storage writes."""
    return TechnologyDetector().detect(
        repo, readme_content=readme_content, manifest_files=manifest_files, topics=topics,
        dependencies=dependencies, pinned_signals=pinned_signals, retrieved_at=retrieved_at,
        raw_record_id=raw_record_id, source_url=source_url, raw_records=raw_records,
    )
