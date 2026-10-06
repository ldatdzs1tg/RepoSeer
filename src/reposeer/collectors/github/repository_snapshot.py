"""GitHub repository snapshot collector."""

from reposeer.collectors.github.repository import RepositoryCollector
from reposeer.schemas.github.repository_snapshot import RepositorySnapshot


class RepositorySnapshotCollector:
    def __init__(self, repo_collector: RepositoryCollector | None = None):
        self.repo_collector = repo_collector or RepositoryCollector()

    def collect_snapshot(self, owner: str, repo: str) -> RepositorySnapshot:
        record = self.repo_collector.collect(owner, repo)
        return RepositorySnapshot(
            repo_full_name=record.full_name,
            stars=record.stars,
            forks=record.forks,
            watchers=record.watchers,
            open_issues=record.open_issues,
            size_kb=record.size,
        )
