"""Unit tests for Pydantic v2 schemas."""

import pytest
from pydantic import ValidationError

from reposeer.clients.external.base import DiscoveryPageResult
from reposeer.schemas.external.candidate import (
    ExternalCandidate,
    evaluate_subject_match,
)
from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.schemas.technology.repo_technology import RepoTechnologyAssociation


def test_repository_record_validation():

    repo = RepositoryRecord(
        owner="pytorch",
        name="pytorch",
        full_name="pytorch/pytorch",
        stargazers_count=80000,
    )
    assert repo.owner == "pytorch"
    assert repo.stars == 80000
    assert repo.archived is False


def test_repository_record_invalid_type():
    with pytest.raises(ValidationError):
        RepositoryRecord(
            owner="pytorch",
            name="pytorch",
            full_name="pytorch/pytorch",
            stargazers_count="not-a-number",  # Invalid type should raise
        )


def test_repo_technology_association():
    assoc = RepoTechnologyAssociation(
        repo_full_name="pytorch/pytorch",
        name="Python",
        source="pyproject.toml",
        confidence=1.0,
    )
    assert assoc.confidence == 1.0
    assert assoc.source == "pyproject.toml"


def test_external_candidate_defaults_and_fields():
    candidate = ExternalCandidate(
        title="Flask 2.0 Released",
        url="https://example.com/flask-2",
        source="hackernews",
    )
    assert candidate.title == "Flask 2.0 Released"
    assert candidate.url == "https://example.com/flask-2"
    assert candidate.source == "hackernews"
    assert candidate.context_level == "package_specific"
    assert candidate.subject_match_status == "ambiguous_requires_crawl"
    assert candidate.matched_queries == []
    assert candidate.source_item_id is None
    assert candidate.snippet is None

    dumped = candidate.model_dump(mode="json")
    assert dumped["context_level"] == "package_specific"
    assert dumped["subject_match_status"] == "ambiguous_requires_crawl"


def test_discovery_page_result():
    res = DiscoveryPageResult(
        candidates=[
            ExternalCandidate(title="T1", url="https://example.com/1", source="hackernews")
        ],
        page_number=1,
        has_more=True,
        total_hits_reported=100,
    )
    assert len(res.candidates) == 1
    assert res.page_number == 1
    assert res.has_more is True
    assert res.total_hits_reported == 100


def test_evaluate_subject_match_rejected_noise():
    assert evaluate_subject_match(url="", title="") == "rejected_noise"
    assert evaluate_subject_match(url="   ", title="  ") == "rejected_noise"
    assert evaluate_subject_match(url=None, title=None) == "rejected_noise"
    assert evaluate_subject_match(url="invalid-not-http", title="") == "rejected_noise"


def test_evaluate_subject_match_exact_url():
    # Canonical GitHub repo URL in candidate url
    assert evaluate_subject_match(
        url="https://github.com/pallets/flask/releases/tag/2.0.0",
        title="Flask Release",
        repo_full_name="pallets/flask",
    ) == "exact_url_match"

    # Canonical GitHub repo URL in snippet
    assert evaluate_subject_match(
        url="https://news.ycombinator.com/item?id=123",
        title="Release Announcement",
        snippet="Check repository at https://github.com/pallets/flask today",
        repo_full_name="pallets/flask",
    ) == "exact_url_match"

    # PyPI project URL in candidate url
    assert evaluate_subject_match(
        url="https://pypi.org/project/flask/",
        title="Flask on PyPI",
        package_name="flask",
    ) == "exact_url_match"

    # known_urls in candidate url
    assert evaluate_subject_match(
        url="https://flask.palletsprojects.com/en/2.0.x/quickstart/",
        title="Flask Quickstart",
        known_urls=["https://flask.palletsprojects.com"],
    ) == "exact_url_match"


def test_evaluate_subject_match_high_confidence_metadata():
    # repo_full_name in title
    assert evaluate_subject_match(
        url="https://dev.to/article/1",
        title="What is new in pallets/flask v2.0?",
        repo_full_name="pallets/flask",
    ) == "high_confidence_metadata"

    # repo_full_name in snippet
    assert evaluate_subject_match(
        url="https://dev.to/article/1",
        title="Modern Python Web Services",
        snippet="In pallets/flask we observe clean routing patterns",
        repo_full_name="pallets/flask",
    ) == "high_confidence_metadata"

    # package name AND primary keyword in title
    assert evaluate_subject_match(
        url="https://dev.to/article/2",
        title="Building microservices with Flask web framework",
        package_name="flask",
        primary_keywords=["web", "microframework"],
    ) == "high_confidence_metadata"


def test_evaluate_subject_match_function_or_alternative_only():
    # function_level with target not named
    assert evaluate_subject_match(
        url="https://example.com/wsgi-benchmarks",
        title="Comparing modern WSGI python servers in 2024",
        context_level="function_level",
        package_name="flask",
        repo_full_name="pallets/flask",
    ) == "function_or_alternative_only"

    # alternative_level with target not named
    assert evaluate_subject_match(
        url="https://example.com/django-review",
        title="Why Django is a great choice for enterprise apps",
        context_level="alternative_level",
        package_name="flask",
        repo_full_name="pallets/flask",
    ) == "function_or_alternative_only"


def test_evaluate_subject_match_ambiguous_requires_crawl():
    # Target named, but no keyword, context_level is package_specific
    assert evaluate_subject_match(
        url="https://example.com/article",
        title="Some thoughts on Flask and developer tooling",
        context_level="package_specific",
        package_name="flask",
        primary_keywords=["microframework"],
    ) == "ambiguous_requires_crawl"

    # Candidate object passed directly
    cand = ExternalCandidate(
        title="Discussion on general web development",
        url="https://news.ycombinator.com/item?id=456",
        source="hackernews",
        context_level="package_specific",
    )
    assert evaluate_subject_match(
        cand,
        repo_full_name="pallets/flask",
        package_name="flask",
    ) == "ambiguous_requires_crawl"

