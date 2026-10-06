"""Mapping contract connecting package entities to GitHub repositories."""

from reposeer.schemas.common import BaseSchema


class PackageRepositoryMapping(BaseSchema):
    """Linkage between an ecosystem package and its upstream GitHub repository."""

    ecosystem: str
    package_name: str
    repo_full_name: str
    repository_url: str
    confidence: float = 1.0
    matched_by: str = "metadata"  # "metadata", "homepage", "heuristic"
