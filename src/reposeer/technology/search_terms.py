"""Bounded, provider-neutral discovery queries for catalog concepts and verified subjects."""

from collections import Counter

from reposeer.contracts import TechnologyEntity
from reposeer.technology.models import SearchIntent, SearchPlatform, SearchTerm
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

        curated_subject = isinstance(primary, TechSpec) and repo_full_name and any(
            repo_full_name.casefold() == known.casefold() for known in primary.known_repo_slugs
        )
        if repo_full_name and (verified_subject or curated_subject):
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
