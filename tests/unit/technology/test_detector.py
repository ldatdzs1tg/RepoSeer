"""Unit tests for the TechnologyDetector."""

from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.technology.detector import TechnologyDetector, detect_technologies


def test_detector_with_repository_record_and_manifests():
    repo = RepositoryRecord(
        id=123456,
        owner="pallets",
        name="flask",
        full_name="pallets/flask",
        description="The Python micro framework for building web applications.",
        language="Python",
        topics=["python", "web", "framework", "flask"],
        stargazers_count=65000,
        forks_count=15000,
    )

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
    to make getting started quick and easy, with the ability to scale up to
    complex applications.

    ## Installing

    Install and update using `pip`:

    ```bash
    pip install -U Flask
    ```
    """

    result = detect_technologies(
        repo=repo,
        readme_content=readme,
        manifest_files=manifests,
        retrieved_at="2024-02-29T12:00:00.000Z",
    )

    # 1. Primary technology
    assert result.primary_spec is not None
    assert result.primary_spec.slug == "flask"
    assert result.primary_spec.name == "Flask"
    assert result.primary_technology is None
    assert result.metadata["repo_id"] is None
    assert result.metadata["supplied_repo_id"] == "github:repository:123456"

    # 2. Identified entities
    detected_slugs = {spec.slug for spec in result.resolved_specs}
    assert "flask" in detected_slugs
    assert "python" in detected_slugs
    assert "werkzeug" in detected_slugs
    assert "jinja2" in detected_slugs
    assert "click" in detected_slugs
    assert "pytest" in detected_slugs

    # Legacy DTO/manual content is useful detection input, but not recorded provenance.
    assert result.entities == []
    assert all(signal.repo_id is None for signal in result.detected_signals)
    assert all(signal.raw_record_ids == [] for signal in result.detected_signals)
    assert any("recorded evidence missing" in diagnostic for diagnostic in result.diagnostics)

    # 4. Search terms
    assert len(result.search_terms) > 0
    flask_terms = [t for t in result.search_terms if t.tech_slug == "flask"]
    assert len(flask_terms) >= 3

    # Check search intents are present
    flask_intents = {t.intent for t in flask_terms}
    assert "announcement" in flask_intents
    assert "maintenance" in flask_intents
    assert "security" in flask_intents


def test_detector_with_pinned_signals():
    result = detect_technologies(
        repo="fastapi/fastapi",
        topics=["api", "async"],
        pinned_signals=["tech:fastapi", "pydantic", "starlette"],
        retrieved_at="2024-02-29T12:00:00.000Z",
    )

    detected_slugs = {spec.slug for spec in result.resolved_specs}
    assert "fastapi" in detected_slugs
    assert "pydantic" in detected_slugs
    assert "starlette" in detected_slugs

    assert result.entities == []
    assert result.primary_spec.slug == "fastapi"
    assert result.confidence_by_slug["fastapi"] == 1.0
    assert "pinned" in result.detection_sources_by_slug["fastapi"]
    assert "pinned" in result.detection_sources_by_slug["pydantic"]
    assert "pinned" in result.detection_sources_by_slug["starlette"]


def test_detector_empty_graceful_handling():
    detector = TechnologyDetector()
    result = detector.detect(repo=None)

    assert result.entities == []
    assert result.search_terms == []
    assert result.primary_technology is None


def test_dictionary_numeric_id_does_not_verify_an_unknown_subject():
    result = detect_technologies(
        {"id": 123456, "full_name": "someone/unrelated-project", "language": "Python"},
        dependencies=["flask"],
    )
    assert result.metadata["repo_id"] is None
    assert result.metadata["supplied_repo_id"] == "github:repository:123456"
    assert all(signal.repo_id is None for signal in result.detected_signals)
    assert {spec.slug for spec in result.resolved_specs} == {"flask", "python"}
    assert result.entities == []
    assert result.search_terms and all("someone/unrelated-project" not in term.query for term in result.search_terms)
    assert any("unverified" in diagnostic for diagnostic in result.diagnostics)
