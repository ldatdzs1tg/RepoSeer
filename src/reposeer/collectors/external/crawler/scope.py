"""Verified current package-to-repository scope, independent of article relevance."""

from dataclasses import dataclass

from reposeer.collectors.package.repository_mapping import PackageRepositoryResolver
from reposeer.contracts import RawRecord, validate_record


@dataclass(frozen=True)
class RepositoryScopeEvidence:
    pypi: RawRecord
    github: RawRecord

    def resolve(self) -> dict:
        pypi = validate_record(self.pypi)
        github = validate_record(self.github)
        if not isinstance(pypi, RawRecord) or not isinstance(github, RawRecord):
            raise ValueError("expected_raw_mapping_evidence")
        if pypi.source != "pypi_json" or not pypi.package_id:
            raise ValueError("expected_pypi_package_evidence")
        package = pypi.package_id.removeprefix("pypi:")
        if pypi.source_url != f"https://pypi.org/pypi/{package}/json":
            raise ValueError("pypi_evidence_url_mismatch")
        if github.source != "github_rest" or github.source_record_type != "github_repository":
            raise ValueError("expected_github_repository_evidence")
        mapping = PackageRepositoryResolver().resolve_current(package, pypi.payload, github.payload)
        if mapping.status != "verified_current" or github.repo_id != mapping.repo_id:
            raise ValueError("unverified_package_repository_mapping")
        if (
            github.source_url.lower()
            != f"https://api.github.com/repos/{github.payload['full_name']}".lower()
        ):
            raise ValueError("github_evidence_url_mismatch")
        if github.payload.get("private") is not False:
            raise ValueError("repository_must_be_verified_public")
        return {
            "package_id": mapping.package_id,
            "repo_id": mapping.repo_id,
            "repository_url": mapping.repository_url,
            "status": mapping.status,
            "basis": "verified_current_discovery_scope",
            "article_relevance": "unverified",
            "historical_validity": "unverified",
            "observed_at": max(pypi.retrieved_at, github.retrieved_at),
            "evidence": [
                {
                    "raw_record_id": r.raw_record_id,
                    "source_url": r.source_url,
                    "retrieved_at": r.retrieved_at,
                }
                for r in (pypi, github)
            ],
        }
