"""Base discovery provider interface according to T-016."""

from abc import ABC, abstractmethod

from reposeer.schemas.external.candidate import ExternalCandidate


class DiscoveryProvider(ABC):
    """Abstract provider for external signals and news discovery."""

    @abstractmethod
    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        """Search upstream provider for articles or mentions."""
        pass
