"""Typed adapters preserving the T-012 collector infrastructure."""

from reposeer.contracts.adapters.github_repository import (
    GitHubRepositoryContractCollector,
    GitHubRepositoryContractNormalizer,
)

__all__ = ["GitHubRepositoryContractCollector", "GitHubRepositoryContractNormalizer"]
