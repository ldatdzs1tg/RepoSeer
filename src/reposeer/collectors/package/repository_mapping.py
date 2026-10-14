"""Evidence-based current PyPI mapping; this does not establish historical association."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from reposeer.contracts.identity import canonical_github_url, package_id, repo_id, validate_http_url
from reposeer.contracts.models import GitHubRepository, RepoIdentity
from reposeer.contracts.validation import validate_record


class CurrentMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    status: Literal["verified_current", "unresolved", "ambiguous"]
    package_id: str
    repo_id: str | None = None
    repository_url: str | None = None
    source_evidence_url: str | None = None
    scope_quality: Literal["repo_only", "unknown"] = "unknown"
    historical_validity: Literal["unverified"] = "unverified"
    reason: str | None = None


def _root_url(value: str) -> str:
    validate_http_url(value)
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "github.com"
            or parsed.port is not None or "?" in value or "#" in value):
        raise ValueError("expected_root_github_url")
    return canonical_github_url(parsed.path.removeprefix("/").removesuffix("/"))


def _identity(repository: GitHubRepository | dict) -> RepoIdentity:
    if isinstance(repository, GitHubRepository):
        return validate_record(repository).identity
    if not isinstance(repository, dict):
        raise ValueError("unverified_github_identity")
    numeric = repository.get("id")
    if type(numeric) is not int or numeric <= 0:
        raise ValueError("unverified_github_identity")
    name = repository.get("full_name")
    url = canonical_github_url(name)
    if repository.get("html_url") is not None and _root_url(repository["html_url"]) != url:
        raise ValueError("github_identity_mismatch")
    return RepoIdentity(repo_id=repo_id(str(numeric)), github_repo_id=str(numeric),
                        github_node_id=None, repo_full_name=name, repo_url=url, known_urls=[])


class PackageRepositoryResolver:
    def resolve_current(self, package_name: str, pypi_metadata: dict,
                        github_repository: GitHubRepository | dict) -> CurrentMapping:
        """Pure resolver: caller supplies provider observations; no network or historical inference."""
        subject = package_id(package_name)

        def unresolved(reason):
            return CurrentMapping(status="unresolved", package_id=subject, reason=reason)

        info = pypi_metadata.get("info") if isinstance(pypi_metadata, dict) else None
        if not isinstance(info, dict):
            return unresolved("missing_pypi_metadata")
        try:
            if package_id(info.get("name")) != subject:
                return unresolved("package_identity_mismatch")
            identity = _identity(github_repository)
        except (ValueError, TypeError):
            return unresolved("unverified_identity")
        urls = info.get("project_urls")
        if not isinstance(urls, dict):
            return unresolved("missing_source_evidence")
        evidence = []
        for label, url in urls.items():
            if not isinstance(label, str) or label.strip().casefold() not in {"source", "repository", "code"}:
                continue
            try:
                if not isinstance(url, str):
                    raise ValueError("invalid_source_url")
                evidence.append((_root_url(url), url))
            except (ValueError, TypeError):
                return unresolved("invalid_source_url")
        if not evidence:
            return unresolved("missing_source_evidence")
        if len({url for url, _ in evidence}) > 1:
            return CurrentMapping(status="ambiguous", package_id=subject, reason="conflicting_source_urls")
        if evidence[0][0] != identity.repo_url:
            return unresolved("repository_mismatch")
        return CurrentMapping(status="verified_current", package_id=subject,
                              repo_id=identity.repo_id, repository_url=identity.repo_url,
                              source_evidence_url=f"https://pypi.org/pypi/{subject.removeprefix('pypi:')}/json",
                              scope_quality="repo_only")

    def map_package(self, ecosystem: str, package_name: str, repo_url: str):
        """Legacy URL-only calls cannot verify a mapping. Use resolve_current instead."""
        raise ValueError("unverified_mapping_requires_source_evidence")
