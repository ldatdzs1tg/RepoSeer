"""Bounded search queries independent of fabricated canonical evidence."""

from collections import Counter

import pytest

from reposeer.technology.registry import TechnologyRegistry
from reposeer.technology.search_terms import SearchTermGenerator


def test_primary_queries_intents_priority_and_platforms():
    registry = TechnologyRegistry()
    specs = [registry.get_by_slug(slug) for slug in ("flask", "python", "werkzeug", "jinja2")]
    generator = SearchTermGenerator()
    terms = generator.generate(entities=specs, repo_full_name="pallets/flask", primary_slug="flask",
                               topics=["python", "web"], verified_subject=True)
    assert {"announcement", "maintenance", "security", "ecosystem"} <= {term.intent for term in terms}
    assert "hackernews" in {term.platform for term in terms}
    assert [term.priority for term in terms] == sorted(term.priority for term in terms)
    assert all(term.tech_slug == "flask" for term in terms if term.priority == 1)
    assert any("pallets/flask maintenance" in term.query for term in terms)
    assert any("Flask Python web framework" in term.query for term in terms)
    assert any(term.query == "Flask release" for term in terms)
    assert all(" OR " not in term.query for term in terms)
    secondary = [term for term in terms if term.tech_slug != "flask"]
    assert len(secondary) <= 1
    assert all(term.intent == "ecosystem" for term in secondary)


def test_casefold_deduplication_determinism_and_query_budget():
    registry = TechnologyRegistry()
    specs = registry.all_specs()
    generator = SearchTermGenerator(max_terms_per_intent=1)
    terms = generator.generate(entities=specs + specs, primary_slug="flask", repo_full_name="pallets/flask")
    assert terms == generator.generate(entities=list(reversed(specs)), primary_slug="flask", repo_full_name="pallets/flask")
    keys = [(term.query.casefold(), term.platform) for term in terms]
    assert len(keys) == len(set(keys))
    assert max(Counter((term.intent, term.platform) for term in terms).values()) == 1
    assert SearchTermGenerator(0).generate(entities=specs, primary_slug="flask") == []
    with pytest.raises(ValueError):
        SearchTermGenerator(-1)


def test_unknown_subject_has_no_invented_technology_or_unverified_queries():
    generator = SearchTermGenerator()
    assert generator.generate(entities=[], repo_full_name="unknown/project") == []
    terms = generator.generate(entities=[], repo_full_name="unknown/project", verified_subject=True)
    assert terms and all(term.tech_slug is None for term in terms)
    assert all("unknown/project" in term.query for term in terms)
    spec = TechnologyRegistry().get_by_slug("requests")
    terms = generator.generate(entities=[spec], repo_full_name="unknown/project")
    assert all("unknown/project" not in term.query for term in terms)
    assert all(term.tech_slug == "requests" for term in terms)


@pytest.mark.parametrize("slug, expected", [
    ("flask", "Flask Python web framework ecosystem discussions"),
    ("requests", "Requests Python library ecosystem discussions"),
])
def test_primary_ecosystem_query_omits_covered_python_topic(slug, expected):
    spec = TechnologyRegistry().get_by_slug(slug)
    terms = SearchTermGenerator().generate(entities=[spec], primary_slug=slug, topics=["python", "PYTHON"])
    assert [term.query for term in terms if term.intent == "ecosystem"] == [expected]
    assert [term.priority for term in terms] == sorted(term.priority for term in terms)
    assert len({(term.query.casefold(), term.platform) for term in terms}) == len(terms)


def test_ecosystem_topics_keep_useful_phrases_and_deterministic_budget():
    spec = TechnologyRegistry().get_by_slug("flask")
    topics = ["python", "wsgi", "HTTP client", "http CLIENT", "web framework"]
    generator = SearchTermGenerator(max_terms_per_intent=1)
    terms = generator.generate(entities=[spec], primary_slug="flask", topics=topics)
    assert terms == generator.generate(entities=[spec], primary_slug="flask", topics=list(reversed(topics)))
    ecosystem = [term for term in terms if term.intent == "ecosystem"]
    assert len(ecosystem) == 1
    assert ecosystem[0].query == "Flask Python web framework HTTP client wsgi ecosystem discussions"
    assert max(Counter((term.intent, term.platform) for term in terms).values()) == 1



def test_curated_name_alone_cannot_generate_verified_exact_repository_queries():
    spec = TechnologyRegistry().get_by_slug("flask")
    generator = SearchTermGenerator()
    manual = generator.generate(entities=[spec], primary_slug="flask", repo_full_name="pallets/flask")
    assert all(term.priority != 1 and "pallets/flask" not in term.query for term in manual)
    verified = generator.generate(entities=[spec], primary_slug="flask", repo_full_name="pallets/flask", verified_subject=True)
    assert any(term.priority == 1 and "pallets/flask" in term.query for term in verified)
    assert all(term.intent != "same_function" for term in verified)
