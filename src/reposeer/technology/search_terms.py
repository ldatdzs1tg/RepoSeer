"""Bounded, provider-neutral discovery queries for catalog concepts and verified subjects."""

from collections import Counter
from typing import Literal

from reposeer.contracts import TechnologyEntity
from reposeer.technology.functional_profile import FunctionalProfileResult
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
    ) -> SearchPlan:
        """Build a deterministic, function-aware search plan for D04 external discovery."""
        if functional_profile is None:
            profile_status: Literal["verified", "unavailable", "not_provided"] = "not_provided"
        elif functional_profile.status == "verified":
            profile_status = "verified"
        else:
            profile_status = "unavailable"

        if primary_spec is None and specs:
            primary_spec = specs[0]
        elif primary_spec is None and entities:
            for item in entities:
                if isinstance(item, TechSpec):
                    primary_spec = item
                    break

        pkg_name = package_id.removeprefix("pypi:") if package_id else None
        repo_short_name = (
            repo_full_name.split("/")[1]
            if (repo_full_name and "/" in repo_full_name)
            else repo_full_name
        )
        subject_name = pkg_name or repo_short_name or (primary_spec.name if primary_spec else None)
        target_slug = primary_spec.slug if primary_spec else None

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
            ambiguous_names = {"requests", "click", "rich", "black", "uv", "pip", "poetry", "wheel", "build"}
            is_ambig = pkg_name.lower() in ambiguous_names or len(pkg_name) <= 3
            if is_ambig:
                disambiguated = f"{pkg_name} Python"
            elif primary_spec and primary_spec.category:
                cat_context = primary_spec.category.replace("_", " ")
                disambiguated = f"{pkg_name} {cat_context}"
            else:
                disambiguated = pkg_name

            add(f"{disambiguated} release", target_slug, "direct_package", "web", priority=1)
            add(f"{disambiguated} release", target_slug, "direct_package", "hackernews", priority=1)
            add(f"{pkg_name} python", target_slug, "direct_package", "stackexchange", priority=2)
            add(f"{pkg_name} python", target_slug, "direct_package", "devto", priority=2)

        # Catalog queries from T-015
        catalog_entities = list(specs or entities or ([primary_spec] if primary_spec else []))
        if catalog_entities:
            t015_terms = self.generate(
                entities=catalog_entities,
                repo_full_name=repo_full_name,
                primary_slug=target_slug,
                topics=topics,
                verified_subject=bool(repo_full_name),
            )
            for t in t015_terms:
                add(t.query, t.tech_slug, t.intent, t.platform, priority=t.priority, context_level="package_specific")

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

            # alternatives (alternative_level)
            cat_desc = ""
            if functional_profile.functional_category and functional_profile.functional_category.text:
                cat_desc = functional_profile.functional_category.text
            elif functional_profile.primary_function and functional_profile.primary_function.text:
                cat_desc = functional_profile.primary_function.text

            for alt in functional_profile.alternative_candidates:
                alt_text = alt.text.strip()
                if not alt_text:
                    continue
                add(f"{alt_text} alternatives", target_slug, "alternatives", "web", priority=2, context_level="alternative_level")
                add(f"{alt_text} alternatives", target_slug, "alternatives", "hackernews", priority=2, context_level="alternative_level")
                if cat_desc:
                    add(f"{alt_text} {cat_desc}", target_slug, "alternatives", "web", priority=2, context_level="alternative_level")

            # migration_adoption (alternative_level)
            sub = subject_name or "package"
            for alt in functional_profile.alternative_candidates:
                alt_text = alt.text.strip()
                if not alt_text:
                    continue
                add(f"{sub} vs {alt_text}", target_slug, "migration_adoption", "web", priority=2, context_level="alternative_level")
                add(f"{sub} vs {alt_text}", target_slug, "migration_adoption", "hackernews", priority=2, context_level="alternative_level")
                add(f"migrate from {sub} to {alt_text}", target_slug, "migration_adoption", "web", priority=2, context_level="alternative_level")
                add(f"migrate from {sub} to {alt_text}", target_slug, "migration_adoption", "devto", priority=2, context_level="alternative_level")

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
            profile_status=profile_status,
            query_version=query_version,
            query_set_hash=query_set_hash,
            terms=final_terms,
        )

