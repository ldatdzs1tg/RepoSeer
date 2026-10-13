"""Public T-015 API resolves curated aliases without inventing unknown concepts."""

from reposeer.contracts import TechnologyEntity as CanonicalTechnologyEntity
from reposeer.technology import TechnologyEntity, TechnologyRegistry, detect_technologies


def test_technology_canonicalization():
    registry = TechnologyRegistry()
    assert TechnologyEntity is CanonicalTechnologyEntity
    assert registry.lookup("pytorch").name == "PyTorch"
    assert registry.lookup("torch").name == "PyTorch"
    assert registry.lookup("docker").name == "Docker"
    assert registry.lookup("unknown_lib") is None


def test_technology_resolver_requirements():
    result = detect_technologies("pallets/flask", manifest_files={
        "requirements.txt": "flask>=2.0.0\ntorch>=2.1.0\nrequests\nunknown_lib"})
    assert {"Flask", "PyTorch", "Requests"} <= {spec.name for spec in result.resolved_specs}
    assert result.entities == []
    assert any("Unrecognized dependency: unknown-lib" in diagnostic for diagnostic in result.diagnostics)
