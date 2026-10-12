"""Integration tests for technology detection and search terms generation."""

import json
from pathlib import Path
import pytest

from reposeer.contracts import (
    Article,
    GitHubRepository,
    TechnologyEntity,
    TechnologyLinkRequest,
    TechnologyLinkResult,
    TechnologyMention,
    validate_record,
    validate_technology_link_pair,
)
from reposeer.technology import detect_technologies

ROOT = Path(__file__).resolve().parents[2]


def example(name: str):
    return json.loads((ROOT / "examples/contracts" / f"{name}.json").read_text(encoding="utf-8"))


def test_flask_sample_repository_end_to_end():
    repo_data = {
        "id": 123456,
        "full_name": "pallets/flask",
        "description": "The Python micro framework for building web applications.",
        "language": "Python",
        "topics": ["python", "web", "framework", "flask", "wsgi", "jinja"],
    }

    manifests = {
        "requirements.txt": """
        Werkzeug>=3.0.0
        Jinja2>=3.1.2
        click>=8.1.3
        itsdangerous>=2.1.2
        blinker>=1.6.2
        pytest>=7.0
        """,
    }

    readme = """
    # Flask

    Flask is a lightweight WSGI web application framework. It is designed
    to make getting started quick and easy.

    ```bash
    pip install Flask
    ```
    """

    result = detect_technologies(
        repo=repo_data,
        readme_content=readme,
        manifest_files=manifests,
        retrieved_at="2024-02-29T12:00:00.000Z",
    )

    # 1. Primary entity
    assert result.primary_technology is not None
    assert result.primary_technology.slug == "flask"
    assert result.primary_technology.name == "Flask"
    assert result.primary_technology.category == "framework"
    assert "github:repository:123456" in result.primary_technology.repo_ids
    assert "pypi:flask" in result.primary_technology.package_ids

    # 2. Schema T-011 validation
    for entity in result.entities:
        assert isinstance(entity, TechnologyEntity)
        assert validate_record(entity.model_dump(mode="json")) == entity

    # 3. Search terms reasonableness
    queries = [t.query for t in result.search_terms]
    # Contains release announcement
    assert any("release announcement" in q for q in queries)
    # Contains maintenance status
    assert any("maintenance status" in q or "maintenance" in q for q in queries)
    # Contains security advisory
    assert any("security vulnerability" in q or "security" in q for q in queries)
    # Contains Hacker News targeted queries
    hn_queries = [t.query for t in result.search_terms if t.platform == "hackernews"]
    assert len(hn_queries) > 0
    assert any("Flask" in q for q in hn_queries)


def test_pydantic_sample_repository_with_canonical_contract():
    # Load canonical GitHubRepository fixture
    repo_fixture = example("github_repository")
    repo = validate_record(repo_fixture)
    assert isinstance(repo, GitHubRepository)

    manifests = {
        "pyproject.toml": """
        [project]
        name = "pydantic"
        dependencies = [
            "pydantic-core==2.14.5",
            "typing-extensions>=4.6.1",
        ]

        [project.optional-dependencies]
        test = ["pytest>=7.4.0"]
        """,
    }

    readme = """
    # Pydantic

    Data validation using Python type hints.

    Fast and extensible, Pydantic plays nicely with your linters/IDE/brain.
    """

    result = detect_technologies(
        repo=repo,
        readme_content=readme,
        manifest_files=manifests,
        retrieved_at="2024-02-29T12:00:00.000Z",
    )

    slugs = {e.slug for e in result.entities}
    assert "pydantic" in slugs
    assert "pytest" in slugs

    # Verify search terms generated for pydantic
    pydantic_terms = [t for t in result.search_terms if t.tech_slug == "pydantic"]
    assert len(pydantic_terms) > 0
    pydantic_queries = [t.query for t in pydantic_terms]
    assert any("pydantic" in q.lower() for q in pydantic_queries)


def test_entities_interoperate_with_t011_technology_link_contract():
    # Detect technologies for sample flask repo
    result = detect_technologies(
        repo="pallets/flask",
        topics=["flask", "python"],
        retrieved_at="2024-02-29T12:00:00.000Z",
    )

    # Use existing T-011 article fixture
    article_dict = example("article")
    article = validate_record(article_dict)
    assert isinstance(article, Article)

    # Build a valid TechnologyLinkRequest under T-011
    link_request_data = {
        "schema_version": "0.1.0",
        "record_type": "technology_link_request",
        "request_id": "00000000-0000-4000-8000-000000000004",
        "run_id": "00000000-0000-4000-8000-000000000001",
        "articles": [article.model_dump(mode="json")],
        "technology_entities": [e.model_dump(mode="json") for e in result.entities],
    }

    # Validate that the link request is valid under T-011
    link_request = validate_record(link_request_data)
    assert isinstance(link_request, TechnologyLinkRequest)

    # Create linked article referencing tech:python
    linked_article = article.model_copy(
        update={
            "technology_mentions": [
                TechnologyMention(
                    technology_id="tech:python",
                    matched_text="Python",
                    confidence=1.0,
                    match_method="manual",
                    extractor_version="0.1.0",
                )
            ]
        }
    )

    link_result_data = {
        "schema_version": "0.1.0",
        "record_type": "technology_link_result",
        "request_id": link_request.request_id,
        "run_id": link_request.run_id,
        "linker_version": "0.1.0",
        "status": "complete",
        "articles": [linked_article.model_dump(mode="json")],
        "errors": [],
    }
    link_result = validate_record(link_result_data)
    assert isinstance(link_result, TechnologyLinkResult)

    # Full boundary pair validation
    validate_technology_link_pair(link_request, link_result)
