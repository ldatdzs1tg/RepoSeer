"""Unit tests for the technology catalog registry and T-011 contract entity generation."""

import pytest

from reposeer.contracts import RawRecord, TechnologyEntity, validate_record
from reposeer.technology.registry import TechnologyRegistry, TechSpec


def catalog_observation(slug="flask"):
    # Synthetic recorded catalog observation for offline contract verification.
    return RawRecord(
        schema_version="0.1.0", record_type="raw_record",
        raw_record_id="raw:00000000-0000-4000-8000-000000000013",
        run_id="00000000-0000-4000-8000-000000000001",
        source="technology_catalog", source_record_type="technology_entity",
        source_record_id=slug, repo_id=None, package_id=None,
        source_url=f"https://example.test/catalog/{slug}",
        retrieved_at="2024-02-29T12:00:00.000Z", event_at=None, available_at=None,
        http_status=None, raw_body_sha256=None, payload={"slug": slug},
    )


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
        package_names=["flask"],
        raw_record=catalog_observation(),
    )

    # Must be instance of canonical TechnologyEntity
    assert isinstance(entity, TechnologyEntity)
    assert entity.schema_version == "0.1.0"
    assert entity.record_type == "technology_entity"
    assert entity.technology_id == "tech:flask"
    assert entity.slug == "flask"
    assert entity.name == "Flask"
    assert entity.category == "framework"
    assert entity.repo_ids == []
    assert "pypi:flask" in entity.package_ids
    assert entity.lifecycle_status == "active"
    assert entity.provenance.source == "technology_catalog"
    assert entity.provenance.temporal_basis == "observation"
    assert entity.provenance.retrieved_at == "2024-02-29T12:00:00.000Z"
    assert entity.provenance.valid_at == "2024-02-29T12:00:00.000Z"
    assert entity.provenance.available_at is None
    assert entity.provenance.raw_record_ids == [catalog_observation().raw_record_id]
    assert entity.provenance.source_url == catalog_observation().source_url

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
        raw_record=catalog_observation("custom-db"),
    )
    assert entity.technology_id == "tech:custom-db"
    assert validate_record(entity) == entity


def test_registry_pinned_ids_and_exact_repository_identity():
    reg = TechnologyRegistry()
    assert reg.lookup("torch").slug == "pytorch"
    assert reg.lookup("tech:fastapi").slug == "fastapi"
    for unknown in ("tech:unknown", "tech:fast_api", "tech:FastAPI", "tech:"):
        assert reg.lookup(unknown) is None
    assert reg.lookup_by_repo("unrelated/requests") is None
    assert reg.lookup_by_repo("requests") is None
    assert reg.lookup_by_repo("PSF/Requests").slug == "requests"
    assert reg.lookup_by_package("some-unknown-dependency") is None
    assert reg.lookup_by_package("python") is None
    assert reg.lookup_by_package("pydantic..__core").slug == "pydantic"


@pytest.mark.parametrize("conflict", ["slug", "alias", "repo", "package"])
def test_registration_conflicts_are_atomic(conflict):
    reg = TechnologyRegistry()
    kwargs = {"slug": "demo", "name": "Demo", "category": "library"}
    if conflict == "slug":
        kwargs["slug"] = "flask"
    elif conflict == "alias":
        kwargs["aliases"] = ["torch"]
    elif conflict == "repo":
        kwargs["known_repo_slugs"] = ["psf/requests"]
    else:
        kwargs["default_packages"] = ["Flask"]
    before = reg.all_specs()
    with pytest.raises(ValueError, match="Conflicting"):
        reg.register(TechSpec(**kwargs))
    assert reg.all_specs() == before
    assert reg.lookup("demo") is None
    assert reg.lookup("torch").slug == "pytorch"
    reg.register(reg.get_by_slug("flask"))  # Idempotent same definition.


def test_no_fabricated_provenance_or_official_url():
    spec = TechnologyRegistry().get_by_slug("flask")
    with pytest.raises(ValueError, match="recorded evidence"):
        spec.to_contract_entity(raw_record_id=catalog_observation().raw_record_id,
                                retrieved_at="2024-02-29T12:00:00.000Z")
    with pytest.raises(ValueError, match="real official URL"):
        TechSpec(slug="demo", name="Demo", category="library").to_contract_entity(
            raw_record=catalog_observation("demo"))
    with pytest.raises(ValueError, match="retrieved_at"):
        spec.to_contract_entity(raw_record=catalog_observation(),
                                retrieved_at="2023-01-01T00:00:00.000Z")


def test_recorded_source_and_metadata_are_preserved():
    raw = catalog_observation()
    raw.source_url = "https://example.test/catalog/flask/observation"
    raw.available_at = "2024-02-28T12:00:00.000Z"
    entity = TechnologyRegistry().get_by_slug("flask").to_contract_entity(raw_record=raw)
    assert entity.provenance.source == raw.source
    assert entity.provenance.source_url == raw.source_url
    assert entity.provenance.retrieved_at == raw.retrieved_at
    assert entity.provenance.available_at == raw.available_at
    assert entity.provenance.raw_record_ids == [raw.raw_record_id]


def test_catalog_identity_and_immutable_spec_collections():
    spec = TechnologyRegistry().get_by_slug("flask")
    with pytest.raises(ValueError, match="does not identify"):
        spec.to_contract_entity(raw_record=catalog_observation("python"))
    aliases = ["demo-alias"]
    custom = TechSpec(slug="demo", name="Demo", category="library", aliases=aliases)
    aliases.append("changed")
    assert custom.aliases == ("demo-alias",)


def test_canonical_catalog_id_and_contradictory_source_identity():
    spec = TechnologyRegistry().get_by_slug("flask")
    raw = catalog_observation()
    raw.payload = {"technology_id": "tech:flask"}
    raw.source_record_id = "tech:flask"
    assert spec.to_contract_entity(raw_record=raw).technology_id == "tech:flask"
    raw.source_record_id = "00000000-0000-4000-8000-000000000013"  # Opaque source key.
    assert spec.to_contract_entity(raw_record=raw).technology_id == "tech:flask"
    raw.source_record_id = "tech:python"
    with pytest.raises(ValueError, match="contradicts"):
        spec.to_contract_entity(raw_record=raw)


@pytest.mark.parametrize("payload", [
    {"name": "Flask"}, {"slug": "flask", "id": "python"},
    {"slug": "tech:flask"}, {"id": "Flask"},
])
def test_catalog_requires_explicit_consistent_canonical_identity(payload):
    raw = catalog_observation()
    raw.payload = payload
    with pytest.raises(ValueError, match="does not identify"):
        TechnologyRegistry().get_by_slug("flask").to_contract_entity(raw_record=raw)


def test_unrelated_documentation_and_provenance_only_are_not_technology_proof():
    spec = TechnologyRegistry().get_by_slug("flask")
    raw = catalog_observation()
    provenance = spec.to_contract_entity(raw_record=raw).provenance
    with pytest.raises(ValueError, match="recorded evidence"):
        spec.to_contract_entity(provenance=provenance)
    for source in ("project_docs", "source_archive"):
        raw.source = source
        raw.source_record_type = "dependency_metadata"
        raw.payload = {"readme_content": "# Python\nPython language documentation."}
        with pytest.raises(ValueError, match="detector-validated"):
            spec.to_contract_entity(raw_record=raw)


def test_repository_evidence_supports_only_observed_technology_and_upstream_id():
    registry = TechnologyRegistry()
    raw = catalog_observation().model_copy(update={
        "source": "github_rest", "source_record_type": "github_repository",
        "source_record_id": "123456", "repo_id": "github:repository:123456",
        "source_url": "https://api.github.com/repos/psf/requests",
        "payload": {"id": 123456, "full_name": "psf/requests", "language": "Python"},
    })
    requests = registry.get_by_slug("requests")
    entity = requests.to_contract_entity(raw_record=raw, repo_numeric_ids=["123456"])
    assert entity.repo_ids == [raw.repo_id]
    assert entity.provenance.raw_record_ids == [raw.raw_record_id]
    assert registry.get_by_slug("python").to_contract_entity(raw_record=raw).repo_ids == []
    with pytest.raises(ValueError, match="does not identify"):
        registry.get_by_slug("flask").to_contract_entity(raw_record=raw)
    raw.payload["topics"] = ["flask"]
    assert registry.get_by_slug("flask").to_contract_entity(raw_record=raw).repo_ids == []
    with pytest.raises(ValueError, match="upstream evidence"):
        registry.get_by_slug("flask").to_contract_entity(raw_record=raw, repo_numeric_ids=["123456"])
    with pytest.raises(ValueError, match="upstream evidence"):
        requests.to_contract_entity(raw_record=raw, repo_numeric_ids=["654321"])
    with pytest.raises(ValueError, match="upstream evidence"):
        requests.to_contract_entity(raw_record=catalog_observation("requests"), repo_numeric_ids=["123456"])
    raw.payload = {"id": 123456, "full_name": "someone/flask", "language": "Python"}
    with pytest.raises(ValueError, match="does not identify"):
        registry.get_by_slug("flask").to_contract_entity(raw_record=raw)
