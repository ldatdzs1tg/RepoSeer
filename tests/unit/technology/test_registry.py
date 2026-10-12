"""Unit tests for the technology catalog registry and T-011 contract entity generation."""

import pytest
from pydantic import ValidationError

from reposeer.contracts import TechnologyEntity, validate_record
from reposeer.technology.registry import TechSpec, TechnologyRegistry


def test_registry_lookups():
    reg = TechnologyRegistry()

    # Exact slug lookup
    flask = reg.get_by_slug("flask")
    assert flask is not None
    assert flask.name == "Flask"
    assert flask.category == "framework"

    # Case-insensitive alias lookup
    assert reg.lookup("FLASK-FRAMEWORK") == flask
    assert reg.lookup("Pallets-Flask") == flask

    # Package distribution name lookup
    assert reg.lookup_by_package("flask") == flask
    pydantic = reg.lookup_by_package("pydantic_core")
    assert pydantic is not None
    assert pydantic.slug == "pydantic"

    # Repo lookup
    assert reg.lookup_by_repo("pallets/flask") == flask
    assert reg.lookup_by_repo("psf/requests") is not None
    assert reg.lookup_by_repo("psf/requests").slug == "requests"

    # Topic lookup
    assert reg.lookup_by_topic("cpython") is not None
    assert reg.lookup_by_topic("cpython").slug == "python"


def test_tech_spec_to_contract_entity_compliance():
    reg = TechnologyRegistry()
    flask_spec = reg.get_by_slug("flask")
    assert flask_spec is not None

    entity = flask_spec.to_contract_entity(
        repo_numeric_ids=["900000001"],
        package_names=["flask"],
        retrieved_at="2024-02-29T12:00:00.000Z",
    )

    # Must be instance of canonical TechnologyEntity
    assert isinstance(entity, TechnologyEntity)
    assert entity.schema_version == "0.1.0"
    assert entity.record_type == "technology_entity"
    assert entity.technology_id == "tech:flask"
    assert entity.slug == "flask"
    assert entity.name == "Flask"
    assert entity.category == "framework"
    assert "github:repository:900000001" in entity.repo_ids
    assert "pypi:flask" in entity.package_ids
    assert entity.lifecycle_status == "active"
    assert entity.provenance.source == "technology_catalog"
    assert entity.provenance.temporal_basis == "observation"
    assert entity.provenance.retrieved_at == "2024-02-29T12:00:00.000Z"
    assert entity.provenance.valid_at == "2024-02-29T12:00:00.000Z"

    # Verify boundary validation passes
    revalidated = validate_record(entity.model_dump(mode="json"))
    assert revalidated == entity


def test_custom_tech_spec_registration_and_validation():
    reg = TechnologyRegistry()
    custom_spec = TechSpec(
        slug="custom-db",
        name="CustomDB",
        category="database",
        aliases=["custom-database", "cdb"],
        description="A specialized in-memory database.",
        official_urls=["https://custom-db.test/"],
        default_packages=["custom-db"],
        keywords=["customdb"],
    )
    reg.register(custom_spec)

    assert reg.lookup("cdb") == custom_spec
    entity = custom_spec.to_contract_entity(
        retrieved_at="2024-02-29T12:00:00.000Z",
    )
    assert entity.technology_id == "tech:custom-db"
    assert validate_record(entity) == entity
