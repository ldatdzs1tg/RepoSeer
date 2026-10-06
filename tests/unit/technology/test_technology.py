"""Unit tests for TechnologyResolver and aliases."""

from reposeer.technology.aliases import canonicalize_technology
from reposeer.technology.resolver import TechnologyResolver


def test_technology_canonicalization():
    assert canonicalize_technology("pytorch") == "PyTorch"
    assert canonicalize_technology("torch") == "PyTorch"
    assert canonicalize_technology("react.js") == "React"
    assert canonicalize_technology("reactjs") == "React"
    assert canonicalize_technology("docker") == "Docker"
    assert canonicalize_technology("unknown_lib") == "unknown_lib"


def test_technology_resolver_requirements():
    resolver = TechnologyResolver()
    content = """
    flask>=2.0.0
    torch>=2.1.0
    requests
    """
    results = resolver.resolve_from_file("pallets/flask", "requirements.txt", content)
    names = [r.name for r in results]

    assert "Flask" in names
    assert "PyTorch" in names
    assert "Requests" in names
