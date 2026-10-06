"""Generic detector fallback."""

from reposeer.schemas.technology.repo_technology import RepoTechnologyAssociation
from reposeer.technology.detectors.base import BaseDetector


class GenericDetector(BaseDetector):
    def can_detect(self, filename: str) -> bool:
        return True

    def detect(
        self, repo_full_name: str, filename: str, content: str
    ) -> list[RepoTechnologyAssociation]:
        return []
