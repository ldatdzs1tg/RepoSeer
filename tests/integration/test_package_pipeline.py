"""Current mapping must be supported by both provider identities, never just a URL."""

import pytest

from reposeer.collectors.package.repository_mapping import PackageRepositoryResolver
from reposeer.schemas.package.package_repository import PackageRepositoryMapping


def resolve(urls, **repository):
    return PackageRepositoryResolver().resolve_current(
        "Demo_Package", {"info": {"name": "demo.package", "project_urls": urls}},
        {"id": 900000001, "full_name": "Owner/MonoRepo", **repository},
    )


def test_exact_mapping_normalizes_identity_without_inventing_scope_or_history():
    result = resolve({"Source": "https://github.com/OWNER/monorepo/"})
    assert result.status == "verified_current"
    assert result.package_id == "pypi:demo-package"
    assert result.repo_id == "github:repository:900000001"
    assert result.repository_url == "https://github.com/owner/monorepo"
    assert result.scope_quality == "repo_only"
    assert result.historical_validity == "unverified"


@pytest.mark.parametrize("urls,status", [
    ({"Source": "https://github.com/owner/monorepo", "Code": "https://github.com/other/repo"}, "ambiguous"),
    ({"Repository": "https://github.com/other/repo"}, "unresolved"),
    ({"Home": "https://github.com/owner/monorepo"}, "unresolved"),
    ({"Source": "owner/monorepo"}, "unresolved"),
    ({"Source": "https://user:password@github.com/owner/monorepo"}, "unresolved"),
    ({"Source": "https://github.com/owner/monorepo/tree/main"}, "unresolved"),
    ({"Source": "https://github.com/owner/monorepo/issues"}, "unresolved"),
])
def test_unproven_mapping_is_not_verified(urls, status):
    result = resolve(urls)
    assert result.status == status and result.repo_id is None
    assert result.historical_validity == "unverified"


@pytest.mark.parametrize("identifier", [None, "900000001", True, 0])
def test_native_repository_requires_numeric_provider_id(identifier):
    assert resolve({"Source": "https://github.com/owner/monorepo"}, id=identifier).status == "unresolved"


def test_package_identity_and_legacy_url_only_calls_fail_closed():
    resolver = PackageRepositoryResolver()
    result = resolver.resolve_current("other", {"info": {"name": "demo", "project_urls": {}}},
                                      {"id": 1, "full_name": "owner/repo"})
    assert result.status == "unresolved"
    with pytest.raises(ValueError, match="unverified_mapping_requires_source_evidence"):
        resolver.map_package("pypi", "demo", "https://github.com/owner/repo")
    legacy = PackageRepositoryMapping(ecosystem="pypi", package_name="demo",
                                     repo_full_name="owner/repo", repository_url="https://github.com/owner/repo")
    assert legacy.confidence is None and legacy.matched_by == "unverified"
