"""Bounded, provider-neutral discovery queries for catalog concepts and verified subjects."""

from collections import Counter
from typing import Literal
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError

from reposeer.contracts import TechnologyEntity
from reposeer.contracts.models import PackageId, RepoId, UtcTimestamp
from reposeer.technology.functional_profile import (
    FunctionalDraft,
    FunctionalProfileResult,
    ground_claims,
)
from reposeer.technology.models import (
    SearchIntent,
    SearchPlan,
    SearchPlatform,
    SearchTerm,
    compute_query_set_hash,
)
from reposeer.technology.registry import TechSpec


class SearchTermGenerator:
    """Budget is per (intent, platform) across the whole generate() call.

    Sort by priority before applying the budget so dependencies cannot displace the primary.
    Secondary concepts receive ecosystem queries only, never repository identity queries.
    """

    def __init__(self, max_terms_per_intent: int = 2):
        if type(max_terms_per_intent) is not int or max_terms_per_intent < 0:
            raise ValueError("max_terms_per_intent must be a nonnegative integer")
        self.max_terms_per_intent = max_terms_per_intent

    def generate(
        self, *, entities: list[TechnologyEntity | TechSpec], repo_full_name: str | None = None,
        primary_slug: str | None = None, topics: list[str] | None = None,
        verified_subject: bool = False,
    ) -> list[SearchTerm]:
        """Accept resolved specs without raw provenance; tech_slug=None is subject-only."""
        by_slug = {entity.slug: entity for entity in entities}
        primary = by_slug.get(primary_slug)
        candidates: list[SearchTerm] = []

        def add(query: str, slug: str | None, intent: SearchIntent,
                platform: SearchPlatform = "web", priority: int = 2):
            query = " ".join(query.split())
            candidates.append(SearchTerm(query=query, tech_slug=slug, intent=intent,
                                         platform=platform, priority=priority, keywords=query.split()))

        if repo_full_name and verified_subject:
            target_slug = primary.slug if primary else None
            add(f"{repo_full_name} release announcement", target_slug, "announcement", priority=1)
            add(f"{repo_full_name} maintenance announcements", target_slug, "maintenance", priority=1)
            add(f"{repo_full_name} security vulnerability advisory", target_slug, "security", priority=1)
        if primary:
            # Category/ecosystem context disambiguates names such as Requests, Click and Rust.
            context = primary.category.replace("_", " ")
            packages = primary.default_packages if isinstance(primary, TechSpec) else primary.package_ids
            if packages:
                context = f"Python {context}"
            name = f"{primary.name} {context}"
            for intent, suffix in (
                ("announcement", "release announcement"), ("maintenance", "maintenance status"),
                ("security", "security vulnerability advisory"), ("deprecation", "deprecation announcement"),
                ("migration", "migration guide upgrade"),
            ):
                add(f"{name} {suffix}", primary.slug, intent)
            for intent, suffix in (("announcement", "release"), ("maintenance", "maintenance"),
                                   ("security", "security")):
                hn_name = f"{primary.name} Python" if primary.slug in {"requests", "click", "rich", "black", "uv"} else primary.name
                add(f"{hn_name} {suffix}", primary.slug, intent, "hackernews")
            if isinstance(primary, TechSpec) and primary.category == "framework":
                if primary.slug in {"flask", "django", "fastapi", "starlette", "tornado"}:
                    context = "Python web framework"
            covered_tokens = set(f"{primary.name} {context}".casefold().split())
            useful_topics = []
            for topic in sorted(set(topics or []), key=lambda t: (t.casefold(), t)):
                tokens = topic.split()
                if not tokens or all(token.casefold() in covered_tokens for token in tokens):
                    continue
                useful_topics.append(" ".join(tokens))
                covered_tokens.update(token.casefold() for token in tokens)
                if len(useful_topics) == 2:
                    break
            topic_context = " ".join(useful_topics)
            add(f"{primary.name} {context} {topic_context} ecosystem discussions", primary.slug, "ecosystem", priority=3)
        for slug in sorted(by_slug):
            if primary and slug == primary.slug:
                continue
            spec = by_slug[slug]
            context = spec.category.replace("_", " ")
            if isinstance(spec, TechSpec) and spec.default_packages:
                context = f"Python {context}"
            query = f"{spec.name} {context} ecosystem discussions"
            if primary:
                query = f"{primary.name} {spec.name} {context} integration discussions"
            add(query, slug, "ecosystem", priority=4)

        candidates.sort(key=lambda t: (t.priority, t.intent, t.platform, t.query.casefold(), t.query, t.tech_slug or ""))
        counts: Counter = Counter()
        seen: set[tuple[str, str]] = set()
        result = []
        for term in candidates:
            key = (term.query.casefold(), term.platform)
            budget_key = (term.intent, term.platform)
            if key in seen or counts[budget_key] >= self.max_terms_per_intent:
                continue
            seen.add(key)
            counts[budget_key] += 1
            result.append(term)
        return result

    def build_search_plan(
        self,
        *,
        specs: list[TechSpec] | None = None,
        primary_spec: TechSpec | None = None,
        repo_full_name: str | None = None,
        package_id: str | None = None,
        repo_id: str | None = None,
        functional_profile: FunctionalProfileResult | None = None,
        topics: list[str] | None = None,
        platforms: list[SearchPlatform] | None = None,
        query_version: str = "1.0.0",
        entities: list[TechnologyEntity | TechSpec] | None = None,
        cutoff_at: str | None = None,
        historical_mode: bool | None = None,
        include_technology_context: bool = False,
    ) -> SearchPlan:
        """Build a deterministic, function-aware search plan for D04 external discovery."""
        diagnostics: list[str] = []
        # Standalone callers with a cutoff must supply current policy explicitly; fail closed otherwise.
        historical = historical_mode if historical_mode is not None else cutoff_at is not None
        if cutoff_at is not None and historical_mode is None:
            diagnostics.append("temporal_mode_unspecified")
        if package_id is not None:
            TypeAdapter(PackageId).validate_python(package_id, strict=True)
        if repo_id is not None:
            TypeAdapter(RepoId).validate_python(repo_id, strict=True)
        if cutoff_at is not None:
            TypeAdapter(UtcTimestamp).validate_python(cutoff_at, strict=True)
        profile_status: Literal["verified", "unavailable", "not_provided"] = "not_provided"
        reason = "profile_not_provided"
        if functional_profile is not None:
            # This is an established D03 backend output, not an independently verified request DTO.
            try:
                functional_profile = FunctionalProfileResult.model_validate(functional_profile.model_dump())
            except ValidationError:
                reason = "profile_invalid"
            else:
                refs = functional_profile.evidence_refs
                fields = {name: getattr(functional_profile, name) for name in FunctionalDraft.model_fields}
                if not functional_profile.alternative_candidates_verified:
                    fields["alternative_candidates"] = []
                draft = FunctionalDraft(**fields)
                _, _, grounding_errors = ground_claims(draft, refs)
                if functional_profile.status != "verified":
                    reason = functional_profile.missing_reason or "profile_unavailable"
                elif (not package_id or not repo_id or functional_profile.package_id != package_id
                      or functional_profile.repo_id != repo_id):
                    reason = "profile_subject_mismatch"
                elif (functional_profile.scope_quality != "target_package" or not refs
                      or not functional_profile.primary_function or not functional_profile.source_revision_keys
                      or any(not any(key.startswith(f"{ref.source_id}@{ref.source_revision}:")
                                     for key in functional_profile.source_revision_keys) for ref in refs)
                      or any(urlsplit(ref.source_url).hostname in {"github.com", "raw.githubusercontent.com"}
                             and not functional_profile.scope_evidence_fingerprints.get(ref.source_id) for ref in refs)):
                    reason = "profile_scope_unverified"
                elif grounding_errors:
                    reason = "profile_claims_unverified"
                elif historical and (
                    cutoff_at is None or functional_profile.cutoff_at != cutoff_at
                    or any(not functional_profile.historical_evidence_fingerprints.get(ref.source_id) for ref in refs)
                ):
                    reason = "profile_historical_unavailable"
                elif not historical and functional_profile.cutoff_at is not None:
                    reason = "profile_temporal_mismatch"
                else:
                    reason = None
                    profile_status = "verified"
            if reason:
                profile_status = "unavailable"
        if reason:
            diagnostics.append(reason)
        if repo_full_name and repo_id is None:
            diagnostics.append("repository_identity_unverified")
        pkg_name = package_id.removeprefix("pypi:") if package_id else None
        target_slug = None  # D04 direct/functional queries do not imply catalog technology association.

        candidates: list[SearchTerm] = []

        def add(
            query: str,
            slug: str | None,
            intent: SearchIntent,
            platform: SearchPlatform = "web",
            priority: int = 2,
            context_level: Literal["package_specific", "function_level", "alternative_level"] = "package_specific",
        ) -> None:
            clean_q = " ".join(query.strip().split())
            if not clean_q:
                return
            candidates.append(
                SearchTerm(
                    query=clean_q,
                    tech_slug=slug,
                    intent=intent,
                    platform=platform,
                    priority=priority,
                    keywords=clean_q.split(),
                    context_level=context_level,
                    query_version=query_version,
                )
            )

        # 1. Direct package queries (always package_specific)
        if repo_full_name:
            add(f"{repo_full_name} release announcement", target_slug, "direct_package", "web", priority=1)
            add(f"{repo_full_name} maintenance status", target_slug, "direct_package", "web", priority=1)
            add(f"{repo_full_name} security advisory", target_slug, "direct_package", "web", priority=1)
            add(f"{repo_full_name}", target_slug, "direct_package", "hackernews", priority=1)
            add(f"{repo_full_name}", target_slug, "direct_package", "stackexchange", priority=2)

        if pkg_name:
            disambiguated = f"{pkg_name} Python package"

            add(f"{disambiguated} release", target_slug, "direct_package", "web", priority=1)
            add(f"{disambiguated} release", target_slug, "direct_package", "hackernews", priority=1)
            add(f"{pkg_name} python", target_slug, "direct_package", "stackexchange", priority=2)
            add(f"{pkg_name} python", target_slug, "direct_package", "devto", priority=2)

        if include_technology_context:
            concepts = [*(specs or []), *(entities or [])]
            if primary_spec is not None:
                concepts.append(primary_spec)
            technology_terms = self.generate(
                entities=concepts, primary_slug=primary_spec.slug if primary_spec else None,
                topics=topics,
            )
            if not technology_terms:
                diagnostics.append("technology_context_unavailable")
            for term in technology_terms:
                candidates.append(term.model_copy(update={
                    "context_level": "technology_level", "query_version": query_version,
                }))

        # 2. D03 Function-aware queries (ONLY when profile_status == "verified")
        if profile_status == "verified" and functional_profile is not None:
            # functional_demand (function_level)
            if functional_profile.primary_function and functional_profile.primary_function.text:
                pf_text = functional_profile.primary_function.text
                add(pf_text, target_slug, "functional_demand", "web", priority=2, context_level="function_level")
                add(pf_text, target_slug, "functional_demand", "hackernews", priority=2, context_level="function_level")
                add(pf_text, target_slug, "functional_demand", "stackexchange", priority=2, context_level="function_level")

            for claim in functional_profile.solved_problems:
                add(claim.text, target_slug, "functional_demand", "web", priority=3, context_level="function_level")
                add(claim.text, target_slug, "functional_demand", "stackexchange", priority=3, context_level="function_level")

            for claim in functional_profile.use_cases:
                add(claim.text, target_slug, "functional_demand", "web", priority=3, context_level="function_level")
                add(claim.text, target_slug, "functional_demand", "devto", priority=3, context_level="function_level")

            for claim in functional_profile.synonyms:
                add(claim.text, target_slug, "functional_demand", "web", priority=3, context_level="function_level")

            # D03 currently exposes only unverified alternative candidates (Literal[False]).
            if functional_profile.alternative_candidates and not functional_profile.alternative_candidates_verified:
                diagnostics.append("alternatives_unverified")

        if platforms:
            platform_set = set(platforms)
            candidates = [t for t in candidates if t.platform in platform_set or t.platform == "all"]

        candidates.sort(
            key=lambda t: (
                t.priority,
                t.intent,
                t.platform,
                t.context_level,
                t.query.casefold(),
                t.query,
                t.tech_slug or "",
            )
        )

        counts: Counter = Counter()
        seen: set[tuple[str, str, str]] = set()
        final_terms: list[SearchTerm] = []
        for term in candidates:
            key = (term.query.casefold(), term.platform, term.intent)
            budget_key = (term.intent, term.platform)
            if key in seen:
                continue
            if self.max_terms_per_intent > 0 and counts[budget_key] >= self.max_terms_per_intent:
                continue
            if self.max_terms_per_intent == 0:
                continue
            seen.add(key)
            counts[budget_key] += 1
            final_terms.append(term)

        query_set_hash = compute_query_set_hash(final_terms)
        return SearchPlan(
            package_id=package_id,
            repo_id=repo_id,
            repo_full_name=repo_full_name,
            include_technology_context=include_technology_context,
            profile_status=profile_status,
            diagnostics=diagnostics,
            query_version=query_version,
            query_set_hash=query_set_hash,
            terms=final_terms,
        )

