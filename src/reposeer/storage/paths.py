"""Storage path resolution according to the project's data architecture."""

from pathlib import Path

from reposeer.config import settings


class StoragePaths:
    """Manages canonical paths for raw, processed, features, and reports data."""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or settings.storage.base_dir
        self.raw_dir = self.base_dir / "raw"
        self.processed_dir = self.base_dir / "processed"
        self.features_dir = self.base_dir / "features"
        self.reports_dir = self.base_dir / "reports"
        self.metadata_dir = self.base_dir / "metadata"

    def raw_path(self, domain: str, entity: str) -> Path:
        """e.g. data/raw/github/repositories.jsonl"""
        target = self.raw_dir / domain / f"{entity}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def processed_path(self, entity: str) -> Path:
        """e.g. data/processed/repositories/repositories.parquet"""
        target = self.processed_dir / entity / f"{entity}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def features_path(self, dataset_name: str) -> Path:
        """e.g. data/features/repository_package_snapshots/snapshots.parquet"""
        target = self.features_dir / dataset_name / f"{dataset_name}.parquet"
        target.parent.mkdir(parents=True, exist_ok=True)
        return target


paths = StoragePaths()
