"""Technology resolver coordinating file inspection and normalization."""

from reposeer.schemas.technology.repo_technology import RepoTechnologyAssociation
from reposeer.technology.detectors.base import BaseDetector
from reposeer.technology.registry import get_default_detectors


class TechnologyResolver:
    """Discovers and normalizes technologies used in repositories."""

    def __init__(self, detectors: list[BaseDetector] | None = None):
        self.detectors = detectors or get_default_detectors()

    def resolve_from_file(
        self, repo_full_name: str, filename: str, content: str
    ) -> list[RepoTechnologyAssociation]:
        """Detect technologies in a single repository file."""
        all_associations: list[RepoTechnologyAssociation] = []
        for detector in self.detectors:
            if detector.can_detect(filename):
                found = detector.detect(repo_full_name, filename, content)
                all_associations.extend(found)
        return all_associations
