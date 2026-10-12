"""Unit tests for search term generation."""

import pytest

from reposeer.technology.registry import TechnologyRegistry
from reposeer.technology.search_terms import SearchTermGenerator


def test_search_terms_multi_intent_and_platform():
    reg = TechnologyRegistry()
    flask_spec = reg.get_by_slug("flask")
    python_spec = reg.get_by_slug("python")
    assert flask_spec and python_spec

    entities = [
        flask_spec.to_contract_entity(
            repo_numeric_ids=["900000001"],
            retrieved_at="2024-02-29T12:00:00.000Z",
        ),
        python_spec.to_contract_entity(
            retrieved_at="2024-02-29T12:00:00.000Z",
        ),
    ]

    generator = SearchTermGenerator()
    terms = generator.generate(
        entities=entities,
        repo_full_name="pallets/flask",
        primary_slug="flask",
        topics=["python", "web", "wsgi", "microframework"],
    )

    assert len(terms) > 0

    # Must contain key intents
    intents = {t.intent for t in terms}
    assert "announcement" in intents
    assert "maintenance" in intents
    assert "security" in intents

    # Must contain both web and hackernews platforms
    platforms = {t.platform for t in terms}
    assert "web" in platforms or "all" in platforms
    assert "hackernews" in platforms

    # Top priority terms should target the primary repo / technology
    top_terms = [t for t in terms if t.priority == 1]
    assert len(top_terms) > 0
    top_queries = [t.query for t in top_terms]
    assert any("pallets/flask" in q or "Flask" in q for q in top_queries)

    # Check Hacker News queries are concise keywords
    hn_terms = [t for t in terms if t.platform == "hackernews"]
    assert any("Flask release" in t.query for t in hn_terms)
    assert any("Flask maintenance" in t.query for t in hn_terms)


def test_search_terms_deduplication():
    reg = TechnologyRegistry()
    spec = reg.get_by_slug("pydantic")
    assert spec
    entity = spec.to_contract_entity(retrieved_at="2024-02-29T12:00:00.000Z")

    generator = SearchTermGenerator()
    terms = generator.generate(entities=[entity], repo_full_name="pydantic/pydantic")

    # Ensure no duplicate (query, platform) pairs exist
    pairs = [(t.query.lower(), t.platform) for t in terms]
    assert len(pairs) == len(set(pairs))
