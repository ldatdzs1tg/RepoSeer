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
from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.technology import TechnologyDetector, detect_technologies

ROOT = Path(__file__).resolve().parents[2]


def example(name: str):
    return json.loads((ROOT / "examples/contracts" / f"{name}.json").read_text(encoding="utf-8"))


def repository_observation(payload):
    raw = example("github_raw_record")
    raw.update(payload=payload, repo_id=f"github:repository:{payload['id']}",
               source_record_id=str(payload["id"]),
               source_url=f"https://api.github.com/repos/{payload['full_name']}")
    return validate_record(raw)


def content_observation(repo_raw, manifests, readme):
    raw = repo_raw.model_dump(mode="json")
    raw.update(raw_record_id="raw:00000000-0000-4000-8000-000000000019",
               source="source_archive", source_record_type="dependency_metadata",
               source_url=f"https://github.com/{repo_raw.payload['full_name']}/tree/main",
               payload={"full_name": repo_raw.payload["full_name"],
                        "manifest_files": manifests, "readme_content": readme})
    return validate_record(raw)


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

    repo_raw = repository_observation(repo_data)
    content_raw = content_observation(repo_raw, manifests, readme)
    result = detect_technologies(
        repo=repo_data,
        readme_content=readme,
        manifest_files=manifests,
        raw_records=[repo_raw, content_raw],
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

    slugs = {e.slug for e in result.resolved_specs}
    assert "pydantic" in slugs
    assert "pytest" in slugs

    assert all(entity.slug != "pytest" for entity in result.entities)
    # Manually supplied dev dependencies are detections, not canonical metadata evidence.
    assert any(signal.technology_id == "tech:pytest" and signal.relationship == "optional_dependency"
               for signal in result.detected_signals)

    # Verify search terms generated for pydantic
    pydantic_terms = [t for t in result.search_terms if t.tech_slug == "pydantic"]
    assert len(pydantic_terms) > 0
    pydantic_queries = [t.query for t in pydantic_terms]
    assert any("pydantic" in q.lower() for q in pydantic_queries)


def test_entities_interoperate_with_t011_technology_link_contract():
    # Detect technologies for sample flask repo
    result = detect_technologies(
        repo=repository_observation({"id": 123456, "full_name": "pallets/flask",
                                     "topics": ["flask", "python"]}),
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


def test_manual_signals_return_specs_without_fabricated_provenance():
    result = detect_technologies("pallets/flask", readme_content="# Flask", pinned_signals=["tech:fastapi"],
                                raw_record_id="raw:00000000-0000-4000-8000-000000000010",
                                retrieved_at="2020-01-01T00:00:00.000Z")
    assert result.primary_spec.slug == "fastapi"
    assert result.primary_technology is None and result.entities == []
    assert result.resolved_specs and result.search_terms
    assert result.metadata["repo_id"] is None and result.metadata["retrieved_at"] is None
    assert all(not signal.raw_record_ids and signal.repo_id is None for signal in result.detected_signals)
    assert any("insufficient evidence" in diagnostic for diagnostic in result.diagnostics)


def test_requests_exact_upstream_and_unrelated_repository():
    result = detect_technologies("unrelated/requests")
    assert result.resolved_specs == [] and result.primary_spec is None
    result = detect_technologies("psf/requests", readme_content="# Requests\nimport requests")
    assert result.primary_spec.slug == "requests"
    assert result.entities == []
    assert any(signal.relationship == "implements" and signal.repo_id is None for signal in result.detected_signals)


def test_missing_and_invalid_numeric_ids_never_become_canonical_repo_ids():
    for numeric in (None, 0, -1, True, "01", 1.5):
        result = detect_technologies({"id": numeric, "full_name": "pallets/flask"}, dependencies=["requests"])
        assert result.metadata["repo_id"] is None
        assert all(signal.repo_id is None for signal in result.detected_signals)
        assert result.entities == []


def test_association_semantics_separate_upstream_runtime_optional_and_mentions():
    payload = {"id": 123456, "full_name": "pallets/flask"}
    manifests = {"pyproject.toml": """
[project]
dependencies = ["werkzeug"]
[project.optional-dependencies]
test = ["pytest"]
[tool.poetry.group.dev.dependencies]
ruff = "*"
"""}
    readme = "We mention Django for reference."
    raw = repository_observation(payload)
    content_raw = content_observation(raw, manifests, readme)
    result = detect_technologies(raw, manifest_files=manifests, readme_content=readme,
                                raw_records=[content_raw])
    relationships = {(signal.technology_id, signal.relationship) for signal in result.detected_signals}
    assert {("tech:flask", "implements"), ("tech:werkzeug", "depends_on"),
            ("tech:pytest", "optional_dependency"), ("tech:ruff", "dev_dependency"),
            ("tech:django", "mentions")} <= relationships
    assert all(signal.repo_id == raw.repo_id and signal.raw_record_ids for signal in result.detected_signals)
    assert next(entity for entity in result.entities if entity.slug == "flask").repo_ids == [raw.repo_id]
    assert all(entity.repo_ids == [] for entity in result.entities if entity.slug != "flask")
    assert all(entity.provenance.available_at is None for entity in result.entities)
    assert next(entity for entity in result.entities if entity.slug == "django").provenance.source == "source_archive"


def test_recorded_timestamp_preserved_without_calling_normalization_clock():
    raw = repository_observation({"id": 123456, "full_name": "psf/requests"})
    def forbidden_clock():
        raise AssertionError("Normalization must not manufacture observation timestamps")
    result = TechnologyDetector(clock=forbidden_clock).detect(
        raw, retrieved_at="2019-01-01T00:00:00.000Z", source_url="https://example.test/override")
    entity = result.primary_technology
    assert entity.provenance.retrieved_at == raw.retrieved_at
    assert entity.provenance.valid_at == raw.retrieved_at
    assert entity.provenance.available_at is None
    assert entity.provenance.source == raw.source and entity.provenance.source_url == raw.source_url
    assert validate_record(entity) == entity


def test_catalog_evidence_does_not_validate_unrelated_manual_signals():
    raw = validate_record(example("technology_raw_record"))  # Catalog identity: Python only.
    result = detect_technologies(pinned_signals=["tech:python", "tech:flask"], raw_records=[raw])
    assert [entity.slug for entity in result.entities] == ["python"]
    assert result.entities[0].provenance.raw_record_ids == [raw.raw_record_id]
    assert result.entities[0].repo_ids == []
    assert result.entities[0].provenance.available_at is None


def test_same_inputs_are_deduplicated_and_deterministic():
    first = detect_technologies("pallets/flask", dependencies=["werkzeug", "werkzeug", "unknown"],
                                topics=["python", "flask"], pinned_signals=["tech:flask", "tech:flask"])
    second = detect_technologies("pallets/flask", dependencies=["unknown", "werkzeug"],
                                 topics=["flask", "python"], pinned_signals=["tech:flask"])
    assert first.resolved_specs == second.resolved_specs
    assert first.search_terms == second.search_terms
    assert first.detected_signals == second.detected_signals
    assert [spec.slug for spec in first.resolved_specs] == [spec.slug for spec in second.resolved_specs]


def test_negative_mentions_do_not_overwrite_manifest_evidence():
    result = detect_technologies("someone/app", dependencies=["flask"],
                                readme_content="Unlike Flask, this does not use Django.")
    assert [spec.slug for spec in result.resolved_specs] == ["flask"]
    assert result.confidence_by_slug["flask"] == 0.90
    assert result.detection_sources_by_slug["flask"] == ["dependency"]
    assert result.primary_spec is None


def test_parse_errors_unknown_pins_and_unrecorded_inputs_remain_observable():
    result = detect_technologies("pallets/flask", manifest_files={"pyproject.toml": "[project"},
                                pinned_signals=["tech:no-such", "tech:fast_api"])
    assert result.entities == []
    assert any("parse error" in diagnostic for diagnostic in result.diagnostics)
    assert sum("Unknown or malformed pinned" in diagnostic for diagnostic in result.diagnostics) == 2


def test_pinned_canonical_entity_keeps_existing_provenance():
    entity = validate_record(example("technology_entity"))
    result = detect_technologies(pinned_signals=[entity])
    assert result.primary_technology == entity
    assert result.primary_technology.provenance == entity.provenance
    invalid = entity.model_copy(update={"technology_id": "tech:wrong"})
    with pytest.raises(ValueError):
        detect_technologies(pinned_signals=[invalid])


def test_cli_local_json_and_file_errors(tmp_path):
    from typer.testing import CliRunner

    from reposeer.cli.app import app

    (tmp_path / "requirements.txt").write_text("flask\nwerkzeug", encoding="utf-8")
    runner = CliRunner()
    response = runner.invoke(app, ["detect-tech", str(tmp_path), "--pinned", "tech:flask", "--json"])
    assert response.exit_code == 0
    data = json.loads(response.stdout)
    assert data["entities"] == [] and data["primary_technology"] is None
    assert data["primary_spec"]["slug"] == "flask"
    assert data["metadata"]["input_mode"] == "local"
    assert data["metadata"]["externally_retrieved"] is False
    response = runner.invoke(app, ["detect-tech", "pallets/flask", "--readme", str(tmp_path / "missing.md")])
    assert response.exit_code != 0
    response = runner.invoke(app, ["detect-tech", "pallets/flask"])
    assert response.exit_code == 0
    assert "not fetched" in response.stdout
    assert "recorded evidence missing" in response.stdout


def test_repository_metadata_does_not_record_manually_supplied_content():
    raw = repository_observation({"id": 123456, "full_name": "psf/requests"})
    manifests = {"requirements.txt": "flask"}
    unrelated_content = content_observation(raw, {"requirements.txt": "django"}, "# Django")
    result = detect_technologies(raw, manifest_files=manifests, readme_content="# Flask",
                                raw_records=[unrelated_content])
    assert "flask" in {spec.slug for spec in result.resolved_specs}
    assert [entity.slug for entity in result.entities] == ["requests"]
    flask_signals = [signal for signal in result.detected_signals if signal.technology_id == "tech:flask"]
    assert flask_signals and all(signal.raw_record_ids == [] for signal in flask_signals)


def test_conflicting_observation_ids_rejected():
    raw = repository_observation({"id": 123456, "full_name": "psf/requests"})
    conflicting = raw.model_copy(update={"payload": {"id": 123456, "full_name": "pallets/flask"}})
    with pytest.raises(ValueError, match="Conflicting observations"):
        detect_technologies(raw, raw_records=[conflicting])


def test_known_availability_retained_and_identity_mapping_validated():
    raw = repository_observation({"id": 123456, "full_name": "psf/requests"})
    raw.available_at = "2024-02-28T12:00:00.000Z"
    result = detect_technologies(raw)
    assert result.primary_technology.provenance.available_at == raw.available_at
    assert result.detected_signals[0].observation_source == raw.source
    assert result.detected_signals[0].retrieved_at == raw.retrieved_at
    assert result.detected_signals[0].available_at == raw.available_at
    mismatch = raw.model_copy(update={"repo_id": "github:repository:654321"})
    with pytest.raises(ValueError, match="disagrees"):
        detect_technologies(mismatch)


@pytest.mark.parametrize("legacy", [False, True])
def test_supplied_repository_identity_needs_matching_raw_evidence(legacy):
    payload = {"id": 123456, "full_name": "pallets/flask", "language": "Python", "topics": ["python"]}
    repo = RepositoryRecord(owner="pallets", name="flask", **payload) if legacy else payload
    unverified = detect_technologies(repo)
    assert unverified.primary_spec.slug == "flask"
    assert unverified.metadata["repo_id"] is None
    assert unverified.entities == []
    assert all(signal.repo_id is None for signal in unverified.detected_signals)
    raw = repository_observation(payload)
    unrelated = repository_observation({"id": 654321, "full_name": "psf/requests"})
    unrelated = unrelated.model_copy(update={"raw_record_id": "raw:00000000-0000-4000-8000-000000000020"})
    verified = detect_technologies(repo, raw_records=[unrelated, raw])
    assert verified.metadata["repo_id"] == raw.repo_id
    assert verified.primary_technology.repo_ids == [raw.repo_id]
    assert {entity.slug for entity in verified.entities} == {"flask", "python"}
    assert all(signal.repo_id == raw.repo_id and signal.raw_record_ids == [raw.raw_record_id]
               for signal in verified.detected_signals)
    for entity in verified.entities:
        assert entity.provenance.source == raw.source
        assert entity.provenance.source_url == raw.source_url
        assert entity.provenance.retrieved_at == raw.retrieved_at
        assert entity.provenance.available_at is None
        assert validate_record(entity) == entity


def test_recorded_repository_identity_conflicts_cannot_verify_supplied_subject():
    payload = {"id": 123456, "full_name": "pallets/flask"}
    for conflicting_payload in (
        {"id": 123456, "full_name": "psf/requests"},
        {"id": 654321, "full_name": "pallets/flask"},
    ):
        raw = repository_observation(conflicting_payload)
        with pytest.raises(ValueError, match="disagrees"):
            detect_technologies(payload, raw_records=[raw])
        original = repository_observation(payload)
        raw = raw.model_copy(update={"raw_record_id": "raw:00000000-0000-4000-8000-000000000020"})
        with pytest.raises(ValueError, match="Conflicting recorded repository identities"):
            detect_technologies(original, raw_records=[raw])
    unrelated = repository_observation({"id": 654321, "full_name": "psf/requests"})
    result = detect_technologies(payload, raw_records=[unrelated])
    assert result.metadata["repo_id"] is None and result.entities == []


def test_canonical_repository_retains_historical_identity_and_provenance():
    data = example("github_repository")
    data["provenance"].update(temporal_basis="historical_reconstruction", valid_at="2023-01-01T00:00:00.000Z")
    repo = validate_record(data)
    result = detect_technologies(repo)
    assert result.metadata["repo_id"] == repo.identity.repo_id
    assert result.metadata["retrieved_at"] == repo.provenance.retrieved_at
    python = next(entity for entity in result.entities if entity.slug == "python")
    assert python.provenance == repo.provenance and python.repo_ids == []
    assert all(signal.repo_id == repo.identity.repo_id for signal in result.detected_signals)
    invalid = repo.model_copy(update={"identity": repo.identity.model_copy(update={"github_repo_id": "123456"})})
    with pytest.raises(ValueError):
        detect_technologies(invalid)


def test_unverified_subject_cannot_borrow_recorded_document_evidence():
    payload = {"id": 123456, "full_name": "someone/app"}
    raw = repository_observation(payload)
    manifests, readme = {"requirements.txt": "flask"}, "# Django"
    content = content_observation(raw, manifests, readme)
    result = detect_technologies(payload, manifest_files=manifests, readme_content=readme, raw_records=[content])
    assert {spec.slug for spec in result.resolved_specs} == {"flask", "django"}
    assert result.metadata["repo_id"] is None and result.entities == []
    assert all(signal.repo_id is None and signal.raw_record_ids == [] for signal in result.detected_signals)
    assert any("recorded evidence missing" in diagnostic for diagnostic in result.diagnostics)


@pytest.mark.parametrize("source", ["source_archive", "project_docs"])
def test_exact_recorded_content_supports_dependencies_and_mentions_for_verified_subject(source):
    raw = repository_observation({"id": 123456, "full_name": "someone/app"})
    manifests, readme = {"requirements.txt": "flask>=3"}, "# Django"
    content = content_observation(raw, manifests, readme).model_copy(update={"source": source})
    result = detect_technologies(raw, manifest_files=manifests, readme_content=readme, raw_records=[content])
    assert {entity.slug for entity in result.entities} == {"flask", "django"}
    assert {(signal.technology_id, signal.relationship) for signal in result.detected_signals} == {
        ("tech:flask", "depends_on"), ("tech:django", "mentions"),
    }
    for entity in result.entities:
        assert entity.repo_ids == []
        assert entity.provenance.source == source
        assert entity.provenance.raw_record_ids == [content.raw_record_id]
        assert entity.provenance.source_url == content.source_url
        assert entity.provenance.retrieved_at == content.retrieved_at
        assert entity.provenance.available_at is None
        assert validate_record(entity) == entity
    wrong_subject = content.model_copy(update={"payload": {**content.payload, "full_name": "someone/other"}})
    mismatched = detect_technologies(raw, manifest_files=manifests, readme_content=readme, raw_records=[wrong_subject])
    assert mismatched.entities == []
    assert all(signal.raw_record_ids == [] for signal in mismatched.detected_signals)


def test_matching_identity_does_not_validate_changed_manual_metadata():
    raw = repository_observation({"id": 123456, "full_name": "someone/app", "language": "Rust",
                                  "topics": ["rust"], "description": "# Rust"})
    manual = {"id": 123456, "full_name": "someone/app", "language": "Python",
              "topics": ["flask"], "description": "# Django"}
    result = detect_technologies(manual, raw_records=[raw])
    assert result.metadata["repo_id"] == raw.repo_id
    assert {spec.slug for spec in result.resolved_specs} == {"python", "flask", "django"}
    assert result.entities == []
    assert all(signal.raw_record_ids == [] for signal in result.detected_signals)


def test_raw_repository_requires_github_source_and_valid_native_identity():
    raw = repository_observation({"id": 123456, "full_name": "pallets/flask"})
    with pytest.raises(ValueError, match="GitHub repository observation"):
        detect_technologies(raw.model_copy(update={"source": "project_docs"}))
    for numeric in (True, 1.5, "01"):
        with pytest.raises(ValueError, match="positive decimal"):
            detect_technologies(raw.model_copy(update={"payload": {**raw.payload, "id": numeric}}))
    with pytest.raises(ValueError, match="owner/repository"):
        detect_technologies(raw.model_copy(update={"payload": {**raw.payload, "full_name": "flask"}}))
    with pytest.raises(ValueError, match="topics must be a list"):
        detect_technologies(raw.model_copy(update={"payload": {**raw.payload, "topics": {"django": True}}}))


def test_detector_rejects_contradictory_catalog_proof_without_losing_concepts():
    raw = validate_record(example("technology_raw_record"))
    raw.payload = {"slug": "flask", "technology_id": "tech:python"}
    result = detect_technologies(pinned_signals=["tech:flask", "tech:python"], raw_records=[raw])
    assert {spec.slug for spec in result.resolved_specs} == {"flask", "python"}
    assert result.entities == []
    assert any("recorded evidence missing" in diagnostic for diagnostic in result.diagnostics)
    assert any("catalog evidence rejected" in diagnostic for diagnostic in result.diagnostics)
