"""Repository operations service."""

from reposeer.collectors.github.repository import RepositoryCollector
from reposeer.schemas.github.repository import RepositoryRecord
from reposeer.storage.paths import paths
from reposeer.storage.processed.parquet import ParquetStorage
from reposeer.storage.raw.jsonl import JsonlStorage


class RepositoryService:
    def __init__(self):
        self.collector = RepositoryCollector()
        self.raw_storage = JsonlStorage()
        self.parquet_storage = ParquetStorage()

    def collect_and_store(self, owner: str, repo: str) -> RepositoryRecord:
        record = self.collector.collect(owner, repo)
        raw_file = paths.raw_path("github", "repositories")
        self.raw_storage.append_record(raw_file, record)

        # Also update processed parquet
        processed_file = paths.processed_path("repositories")
        existing = self.raw_storage.read(raw_file)
        self.parquet_storage.write(processed_file, existing)
        return record
