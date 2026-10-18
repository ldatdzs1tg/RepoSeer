"""Unit tests for T-016 Function-Aware Query Planning and Query Set Hash."""

import hashlib

import pytest

from reposeer.technology.functional_profile import (
    FunctionalProfileResult,
    FunctionalSource,
)
from reposeer.technology.functional_profiler import FunctionalProfiler
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
    profile = _grounded_profile()

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
    assert {"direct_package", "functional_demand"} <= intents
    assert not intents & {"alternatives", "migration_adoption"}
    assert "alternatives_unverified" in plan.diagnostics

    for term in plan.terms:
        assert term.query_id is not None
        assert term.query_id.startswith("query:")
        if term.intent == "direct_package":
            assert term.context_level == "package_specific"
        elif term.intent == "functional_demand":
            assert term.context_level == "function_level"
        elif term.intent in ("alternatives", "migration_adoption"):
            assert term.context_level == "alternative_level"



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


def _grounded_profile(cutoff_at=None):
    text = "Lightweight WSGI web application framework. Django is an unverified alternative candidate."
    document = FunctionalSource(source_id="description", source_path="info.description", source_revision="3.0.0",
                                source_available_at="2020-01-01T00:00:00.000Z" if cutoff_at else None,
                                availability_basis="versioned_archive" if cutoff_at else "current_observation",
                                availability_evidence_ref=None, retrieved_at="2024-02-20T00:00:00.000Z",
                                text=text, scope="target_package", source_url="https://pypi.org/pypi/flask/3.0.0/json")

    class ExtractiveLLM:
        model_id = "offline-test"

        def generate(self, **kwargs):
            span = kwargs["spans"][0]
            claim = {"text": "Lightweight WSGI web application framework.", "evidence_span_ids": [span.span_id]}
            return dict(primary_function=claim, solved_problems=[], capabilities=[], use_cases=[], functional_category=claim,
                        synonyms=[], alternative_candidates=[{"text": "Django", "evidence_span_ids": [span.span_id]}])

    # Fixture-owned immutable capture manifest, separate from the D03 request metadata.
    manifest = (document.source_id, document.source_url, document.source_revision,
                hashlib.sha256(text.encode("utf-8")).hexdigest(), "2020-01-01T00:00:00.000Z")

    class ArchiveVerifier:
        def verify(self, source, asof):
            observed = (source.source_id, source.source_url, source.source_revision,
                        hashlib.sha256(source.text.encode("utf-8")).hexdigest(), source.source_available_at)
            return hashlib.sha256(repr(manifest).encode()).hexdigest() if observed == manifest and manifest[-1] <= asof else None

    return FunctionalProfiler().build_profile(package_id="pypi:flask", repo_id="github:repository:12345",
                                              package_scope=None, sources=[document], llm=ExtractiveLLM(),
                                              cutoff_at=cutoff_at, historical_verifier=ArchiveVerifier() if cutoff_at else None)


@pytest.mark.parametrize("change,reason", [
    ({"package_id": "pypi:django"}, "profile_subject_mismatch"),
    ({"repo_id": "github:repository:98765"}, "profile_subject_mismatch"),
    ({"scope_quality": "repo_only"}, "profile_scope_unverified"),
    ({"source_revision_keys": []}, "profile_scope_unverified"),
])
def test_ineligible_profile_has_direct_only_fallback(change, reason):
    profile = _grounded_profile().model_copy(update=change)
    plan = SearchTermGenerator().build_search_plan(package_id="pypi:flask", repo_id="github:repository:12345",
                                                   repo_full_name="pallets/flask", functional_profile=profile)
    assert {term.intent for term in plan.terms} == {"direct_package"}
    assert reason in plan.diagnostics and plan.profile_status == "unavailable"


def test_current_profile_cannot_be_used_for_historical_cutoff_or_missing_identity():
    generator = SearchTermGenerator()
    profile = _grounded_profile()
    plan = generator.build_search_plan(package_id="pypi:flask", repo_id="github:repository:12345",
                                       functional_profile=profile, cutoff_at="2022-01-01T00:00:00.000Z", historical_mode=True)
    assert "profile_historical_unavailable" in plan.diagnostics
    assert all(term.intent == "direct_package" for term in plan.terms)
    for identity in ({"package_id": "pypi:flask"}, {"repo_id": "github:repository:12345", "repo_full_name": "pallets/flask"}):
        plan = generator.build_search_plan(functional_profile=profile, **identity)
        assert "profile_subject_mismatch" in plan.diagnostics
        assert all(term.intent == "direct_package" for term in plan.terms)


def test_profile_claim_mutation_cannot_supply_ungrounded_function_queries():
    from reposeer.technology.functional_profile import FunctionalClaim

    profile = _grounded_profile().model_copy(update={"primary_function": FunctionalClaim(
        text="Invented user problem", evidence_span_ids=["span:missing"])})
    plan = SearchTermGenerator().build_search_plan(package_id="pypi:flask", repo_id="github:repository:12345", functional_profile=profile)
    assert "profile_claims_unverified" in plan.diagnostics
    assert all(term.intent == "direct_package" for term in plan.terms)


def test_matching_historical_d03_output_is_eligible_only_for_its_verified_cutoff():
    cutoff = "2022-01-01T00:00:00.000Z"
    profile = _grounded_profile(cutoff_at=cutoff)
    assert profile.status == "verified" and profile.cutoff_at == cutoff
    assert profile.historical_evidence_fingerprints
    generator = SearchTermGenerator()
    inputs = dict(package_id="pypi:flask", repo_id="github:repository:12345", functional_profile=profile, historical_mode=True)
    eligible = generator.build_search_plan(cutoff_at=cutoff, **inputs)
    assert eligible.profile_status == "verified" and any(term.intent == "functional_demand" for term in eligible.terms)
    mismatched = generator.build_search_plan(cutoff_at="2023-01-01T00:00:00.000Z", **inputs)
    assert "profile_historical_unavailable" in mismatched.diagnostics
    assert all(term.intent == "direct_package" for term in mismatched.terms)


@pytest.mark.parametrize("slug,repo", [("flask", "pallets/flask"), ("requests", "psf/requests")])
def test_technology_context_is_opt_in_bounded_and_deterministic(slug, repo):
    generator = SearchTermGenerator()
    spec = TechnologyRegistry().get_by_slug(slug)
    assert spec is not None
    kwargs = dict(repo_full_name=repo, specs=[spec, spec], primary_spec=spec)
    direct = generator.build_search_plan(**kwargs)
    enriched = generator.build_search_plan(**kwargs, include_technology_context=True)
    assert all(term.context_level == "package_specific" for term in direct.terms)
    technology = [term for term in enriched.terms if term.context_level == "technology_level"]
    assert technology and all(term.tech_slug == slug for term in technology)
    expected = generator.generate(entities=[spec], primary_slug=slug)
    assert {term.query for term in technology} == {term.query for term in expected}
    assert all(repo not in term.query for term in technology)
    keys = [(term.query.casefold(), term.platform, term.intent) for term in enriched.terms]
    assert len(keys) == len(set(keys))
    assert enriched.query_set_hash != direct.query_set_hash
    assert enriched.model_dump() == generator.build_search_plan(**kwargs, include_technology_context=True).model_dump()


def test_technology_context_without_recognized_concepts_keeps_direct_plan():
    generator = SearchTermGenerator()
    direct = generator.build_search_plan(package_id="pypi:unknown-package")
    enriched = generator.build_search_plan(package_id="pypi:unknown-package", include_technology_context=True)
    assert enriched.terms == direct.terms and enriched.query_set_hash == direct.query_set_hash
    assert enriched.include_technology_context
    assert "technology_context_unavailable" in enriched.diagnostics
