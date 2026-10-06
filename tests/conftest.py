"""Shared Pytest fixtures."""

from pathlib import Path

import pytest

from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.storage.paths import StoragePaths


@pytest.fixture
def tmp_storage(tmp_path: Path) -> StoragePaths:
    """Fixture providing temporary storage directories."""
    return StoragePaths(base_dir=tmp_path)


@pytest.fixture
def sample_repo_record() -> RepositoryRecord:
    """Fixture with sample repository metadata."""
    return RepositoryRecord(
        id=123456,
        owner="pallets",
        name="flask",
        full_name="pallets/flask",
        description="The Python micro framework for building web applications.",
        stargazers_count=65000,
        forks_count=15000,
        language="Python",
        topics=["python", "web", "framework", "flask"],
        archived=False,
    )
