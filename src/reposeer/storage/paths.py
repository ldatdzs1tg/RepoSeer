"""Storage path resolution according to the project's data architecture."""

from pathlib import Path

from pydantic import TypeAdapter

from reposeer.config import settings
from reposeer.contracts.models import UuidText


class StoragePaths:
    """Manages canonical paths for raw, processed, features, and reports data."""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = Path(base_dir if base_dir is not None else settings.storage.base_dir)
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

    def processing_manifest_path(self, processing_run_id: str) -> Path:
        TypeAdapter(UuidText).validate_python(processing_run_id, strict=True)
        return self.metadata_dir / "collection_runs" / f"processing_{processing_run_id}.json"

    def processing_inputs(self) -> list[Path]:
        """Discover existing normalized-source inputs only; never discovery candidates."""
        inputs = [self.raw_dir / "github" / f"{name}.jsonl" for name in
                  ("repositories", "commits", "issues", "pull_requests", "contributors", "releases")]
        if (self.base_dir / "COLLECTION_INPUT.json").exists() and (self.base_dir / "READY.json").exists():
            from reposeer.storage.external_inputs import resolve_verified_external_inputs

            inputs.extend(resolve_verified_external_inputs(self.base_dir)["document_paths"])
        else:
            inputs.append(self.raw_dir / "external" / "documents.jsonl")
        return [path for path in inputs if path.is_file()]


paths = StoragePaths()
