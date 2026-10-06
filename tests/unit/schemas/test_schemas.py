"""Unit tests for Pydantic v2 schemas."""

import pytest
from pydantic import ValidationError

from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.schemas.technology.repo_technology import RepoTechnologyAssociation


def test_repository_record_validation():
    repo = RepositoryRecord(
        owner="pytorch",
        name="pytorch",
        full_name="pytorch/pytorch",
        stargazers_count=80000,
    )
    assert repo.owner == "pytorch"
    assert repo.stars == 80000
    assert repo.archived is False


def test_repository_record_invalid_type():
    with pytest.raises(ValidationError):
        RepositoryRecord(
            owner="pytorch",
            name="pytorch",
            full_name="pytorch/pytorch",
            stargazers_count="not-a-number",  # Invalid type should raise
        )


def test_repo_technology_association():
    assoc = RepoTechnologyAssociation(
        repo_full_name="pytorch/pytorch",
        name="Python",
        source="pyproject.toml",
        confidence=1.0,
    )
    assert assoc.confidence == 1.0
    assert assoc.source == "pyproject.toml"
