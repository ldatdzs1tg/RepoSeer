"""Unit tests for T-016 Function-Aware Query Planning and Query Set Hash."""

from reposeer.technology.functional_profile import (
    FunctionalClaim,
    FunctionalProfileResult,
)
from reposeer.technology.models import (
    SearchPlan,
    SearchTerm,
    compute_query_set_hash,
)
from reposeer.technology.registry import TechnologyRegistry
from reposeer.technology.search_terms import SearchTermGenerator


def test_search_term_query_id_generation():
    term = SearchTerm(
        query="pallets/flask release",
        intent="direct_package",
        platform="hackernews",
    )
    assert term.query_id is not None
    assert term.query_id.startswith("query:")
    assert len(term.query_id) == len("query:") + 16

    # Custom query_id is preserved
    term_custom = SearchTerm(
        query="pallets/flask release",
        intent="direct_package",
        platform="hackernews",
        query_id="custom-id-99",
    )
    assert term_custom.query_id == "custom-id-99"


def test_build_search_plan_with_verified_functional_profile():
    claim_func = FunctionalClaim(
        text="lightweight WSGI web application framework",
        evidence_span_ids=["span:1"],
    )
    claim_prob = FunctionalClaim(
        text="building web APIs and microservices",
        evidence_span_ids=["span:2"],
    )
    claim_use = FunctionalClaim(
        text="REST API development",
        evidence_span_ids=["span:3"],
    )
    claim_syn = FunctionalClaim(
        text="Python microframework",
        evidence_span_ids=["span:4"],
    )
    claim_cat = FunctionalClaim(
        text="web framework",
        evidence_span_ids=["span:5"],
    )
    claim_alt1 = FunctionalClaim(
        text="FastAPI",
        evidence_span_ids=["span:6"],
    )
    claim_alt2 = FunctionalClaim(
        text="Django",
        evidence_span_ids=["span:7"],
    )

    profile = FunctionalProfileResult(
        status="verified",
        package_id="pypi:flask",
        repo_id="github:repository:12345",
        primary_function=claim_func,
        solved_problems=[claim_prob],
        use_cases=[claim_use],
        synonyms=[claim_syn],
        functional_category=claim_cat,
        alternative_candidates=[claim_alt1, claim_alt2],
    )

    generator = SearchTermGenerator(max_terms_per_intent=3)
    spec = TechnologyRegistry().get_by_slug("flask")

    plan: SearchPlan = generator.build_search_plan(
        repo_full_name="pallets/flask",
        package_id="pypi:flask",
        repo_id="github:repository:12345",
        primary_spec=spec,
        functional_profile=profile,
    )

    assert plan.profile_status == "verified"
    assert plan.repo_full_name == "pallets/flask"
    assert plan.package_id == "pypi:flask"
    assert len(plan.query_set_hash) == 64

    intents = {term.intent for term in plan.terms}
    assert {"direct_package", "functional_demand", "alternatives", "migration_adoption"} <= intents

    for term in plan.terms:
        assert term.query_id is not None
        assert term.query_id.startswith("query:")
        if term.intent == "direct_package":
            assert term.context_level == "package_specific"
        elif term.intent == "functional_demand":
            assert term.context_level == "function_level"
        elif term.intent in ("alternatives", "migration_adoption"):
            assert term.context_level == "alternative_level"

    # Verify migration terms pair subject with alternative
    migration_terms = [t.query for t in plan.terms if t.intent == "migration_adoption"]
    assert any("flask vs FastAPI" in q or "flask vs fastapi" in q.lower() for q in migration_terms)
    assert any("migrate from flask to" in q.lower() for q in migration_terms)


def test_build_search_plan_with_unavailable_or_none_functional_profile():
    generator = SearchTermGenerator()
    spec = TechnologyRegistry().get_by_slug("flask")

    # 1. functional_profile = None
    plan_none = generator.build_search_plan(
        repo_full_name="pallets/flask",
        package_id="pypi:flask",
        primary_spec=spec,
        functional_profile=None,
    )
    assert plan_none.profile_status == "not_provided"
    assert len(plan_none.terms) > 0
    assert all(t.context_level == "package_specific" for t in plan_none.terms)
    assert not any(t.context_level in ("function_level", "alternative_level") for t in plan_none.terms)
    assert not any(t.intent in ("functional_demand", "alternatives", "migration_adoption") for t in plan_none.terms)

    # 2. functional_profile.status = "unavailable"
    profile_unavail = FunctionalProfileResult(
        status="unavailable",
        package_id="pypi:flask",
        repo_id="github:repository:12345",
        missing_reason="historical_revision_unverified",
    )
    plan_unavail = generator.build_search_plan(
        repo_full_name="pallets/flask",
        package_id="pypi:flask",
        primary_spec=spec,
        functional_profile=profile_unavail,
    )
    assert plan_unavail.profile_status == "unavailable"
    assert len(plan_unavail.terms) > 0
    assert all(t.context_level == "package_specific" for t in plan_unavail.terms)
    assert not any(t.context_level in ("function_level", "alternative_level") for t in plan_unavail.terms)
    assert not any(t.intent in ("functional_demand", "alternatives", "migration_adoption") for t in plan_unavail.terms)


def test_query_set_hash_determinism_and_sensitivity():
    terms = [
        SearchTerm(query="pallets/flask release", intent="direct_package", platform="hackernews", context_level="package_specific"),
        SearchTerm(query="Flask Python web framework", intent="direct_package", platform="web", context_level="package_specific"),
        SearchTerm(query="FastAPI alternatives", intent="alternatives", platform="web", context_level="alternative_level"),
    ]

    hash_original = compute_query_set_hash(terms)
    assert len(hash_original) == 64

    # Order independence: reversed terms produce identical hash
    hash_reversed = compute_query_set_hash(list(reversed(terms)))
    assert hash_original == hash_reversed

    # Sensitivity: altering query string changes hash
    modified_query = [
        terms[0].model_copy(update={"query": "pallets/flask release announcement"}),
        terms[1],
        terms[2],
    ]
    assert compute_query_set_hash(modified_query) != hash_original

    # Sensitivity: altering query_version changes hash
    modified_version = [t.model_copy(update={"query_version": "2.0.0"}) for t in terms]
    assert compute_query_set_hash(modified_version) != hash_original

    # Sensitivity: altering platform changes hash
    modified_platform = [
        terms[0].model_copy(update={"platform": "web"}),
        terms[1],
        terms[2],
    ]
    assert compute_query_set_hash(modified_platform) != hash_original


def test_search_plan_with_platform_filter():
    generator = SearchTermGenerator()
    plan = generator.build_search_plan(
        repo_full_name="pallets/flask",
        package_id="pypi:flask",
        platforms=["hackernews", "stackexchange"],
    )
    assert len(plan.terms) > 0
    for term in plan.terms:
        assert term.platform in ("hackernews", "stackexchange", "all")
