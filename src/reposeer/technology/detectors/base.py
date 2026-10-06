"""Base technology detector specification."""

from abc import ABC, abstractmethod

from reposeer.schemas.technology.repo_technology import RepoTechnologyAssociation


class BaseDetector(ABC):
    """Abstract interface for rule-based dependency detectors."""

    @abstractmethod
    def can_detect(self, filename: str) -> bool:
        """Check if detector handles this specific filename."""
        pass

    @abstractmethod
    def detect(
        self, repo_full_name: str, filename: str, content: str
    ) -> list[RepoTechnologyAssociation]:
        """Parse file content and extract detected technologies."""
        pass
