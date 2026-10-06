"""Java detector stub."""

from reposeer.schemas.technology.repo_technology import RepoTechnologyAssociation
from reposeer.technology.detectors.base import BaseDetector


class JavaDetector(BaseDetector):
    def can_detect(self, filename: str) -> bool:
        return filename in ("pom.xml", "build.gradle")

    def detect(
        self, repo_full_name: str, filename: str, content: str
    ) -> list[RepoTechnologyAssociation]:
        return []
