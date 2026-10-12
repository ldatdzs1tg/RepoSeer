"""Search term generation for external information discovery."""

from typing import Any

from reposeer.contracts import TechnologyEntity
from reposeer.technology.models import SearchIntent, SearchPlatform, SearchTerm


class SearchTermGenerator:
    """Generates prioritized, multi-intent search queries for external information search."""

    def __init__(self, max_terms_per_intent: int = 2):
        self.max_terms_per_intent = max_terms_per_intent

    def generate(
        self,
        *,
        entities: list[TechnologyEntity],
        repo_full_name: str | None = None,
        primary_slug: str | None = None,
        topics: list[str] | None = None,
    ) -> list[SearchTerm]:
        """Generate deduplicated, multi-platform search terms."""
        search_terms: list[SearchTerm] = []
        seen_queries: set[str] = set()

        def add_term(
            query: str,
            tech_slug: str,
            intent: SearchIntent,
            platform: SearchPlatform = "all",
            priority: int = 3,
            keywords: list[str] | None = None,
        ):
            clean_q = " ".join(query.strip().split())
            query_key = (clean_q.casefold(), platform)
            if query_key not in seen_queries:
                seen_queries.add(query_key)
                search_terms.append(
                    SearchTerm(
                        query=clean_q,
                        tech_slug=tech_slug,
                        intent=intent,
                        platform=platform,
                        priority=priority,
                        keywords=keywords or clean_q.split(),
                    )
                )

        # 1. Primary repository-level terms
        repo_name_only = repo_full_name.split("/")[-1] if repo_full_name and "/" in repo_full_name else repo_full_name
        primary_entity = None
        if primary_slug:
            primary_entity = next((e for e in entities if e.slug == primary_slug), None)
        if not primary_entity and entities:
            primary_entity = entities[0]

        target_slug = primary_entity.slug if primary_entity else (repo_name_only.lower() if repo_name_only else "general")

        if repo_full_name:
            # Identity & Announcement
            add_term(
                f'"{repo_full_name}" release announcement',
                tech_slug=target_slug,
                intent="announcement",
                platform="all",
                priority=1,
            )
            # Maintenance & Health
            add_term(
                f'"{repo_full_name}" maintenance OR deprecated OR unmaintained',
                tech_slug=target_slug,
                intent="maintenance",
                platform="web",
                priority=1,
            )
            # Security Advisory
            add_term(
                f'"{repo_full_name}" security vulnerability OR CVE advisory',
                tech_slug=target_slug,
                intent="security",
                platform="web",
                priority=1,
            )

        # 2. Entity-level terms for each detected technology
        for entity in entities:
            slug = entity.slug
            name = entity.name
            aliases = entity.aliases
            alias_or_name = f'"{name}"' if " " in name else name

            # Is this the primary subject?
            is_primary = primary_entity and entity.slug == primary_entity.slug
            base_prio = 1 if is_primary else 3

            # --- Intent: Announcement ---
            add_term(
                f"{alias_or_name} release announcement",
                tech_slug=slug,
                intent="announcement",
                platform="all",
                priority=base_prio,
            )
            add_term(
                f"{alias_or_name} major version changelog",
                tech_slug=slug,
                intent="announcement",
                platform="web",
                priority=base_prio + 1,
            )

            # --- Intent: Maintenance ---
            add_term(
                f"{alias_or_name} maintenance status",
                tech_slug=slug,
                intent="maintenance",
                platform="all",
                priority=base_prio,
            )
            add_term(
                f"{alias_or_name} abandoned OR deprecated",
                tech_slug=slug,
                intent="maintenance",
                platform="web",
                priority=base_prio,
            )

            # --- Intent: Security ---
            add_term(
                f"{alias_or_name} security vulnerability advisory",
                tech_slug=slug,
                intent="security",
                platform="all",
                priority=base_prio,
            )

            # --- Intent: Deprecation / Migration ---
            add_term(
                f"{alias_or_name} deprecation breaking changes",
                tech_slug=slug,
                intent="deprecation",
                platform="web",
                priority=base_prio + 1,
            )
            add_term(
                f"{alias_or_name} migration guide upgrade",
                tech_slug=slug,
                intent="migration",
                platform="web",
                priority=base_prio + 1,
            )

            # --- Platform-specific: Hacker News ---
            # Hacker News search performs best with concise keyword queries
            if repo_name_only and repo_name_only.lower() != name.lower():
                add_term(
                    f"{repo_name_only} {name}",
                    tech_slug=slug,
                    intent="general",
                    platform="hackernews",
                    priority=base_prio,
                )
            add_term(
                f"{name} release",
                tech_slug=slug,
                intent="announcement",
                platform="hackernews",
                priority=base_prio,
            )
            add_term(
                f"{name} maintenance",
                tech_slug=slug,
                intent="maintenance",
                platform="hackernews",
                priority=base_prio,
            )
            add_term(
                f"{name} security",
                tech_slug=slug,
                intent="security",
                platform="hackernews",
                priority=base_prio,
            )

            # If aliases are available, add alias search
            if aliases:
                primary_alias = aliases[0]
                add_term(
                    f"{primary_alias} release notes",
                    tech_slug=slug,
                    intent="announcement",
                    platform="web",
                    priority=base_prio + 1,
                )

        # 3. Topic-augmented queries
        if topics and primary_entity:
            relevant_topics = [t for t in topics if t.lower() not in {primary_entity.name.lower(), primary_entity.slug.lower()}]
            if relevant_topics:
                topic_str = " ".join(relevant_topics[:3])
                add_term(
                    f"{primary_entity.name} {topic_str} ecosystem",
                    tech_slug=primary_entity.slug,
                    intent="ecosystem",
                    platform="web",
                    priority=3,
                )

        # Sort by priority (1 is highest)
        search_terms.sort(key=lambda t: t.priority)
        return search_terms
