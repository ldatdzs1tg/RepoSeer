"""Explicit T-019 projections and idempotent, per-file atomic processed persistence."""

import json
from typing import TYPE_CHECKING, Any

import polars as pl
from pydantic import TypeAdapter

from reposeer.collectors.github.repository import repository_exclusion_reasons
from reposeer.contracts import GitHubRepository, validate_record
from reposeer.contracts.models import UuidText
from reposeer.exceptions import StorageError
from reposeer.storage.manifest import DatasetWrite
from reposeer.storage.paths import StoragePaths
from reposeer.storage.processed.parquet import ParquetStorage

if TYPE_CHECKING:
    from reposeer.pipelines.normalization import NormalizationPipelineResult


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _schema(names: str, *, booleans: str = "", integers: str = "") -> dict:
    schema = dict.fromkeys(names.split(), pl.String)
    schema.update(dict.fromkeys(booleans.split(), pl.Boolean))
    schema.update(dict.fromkeys(integers.split(), pl.Int64))
    return schema


DATASET_SCHEMAS = {
    "repositories": _schema(
        "schema_version repo_id github_repo_id repo_full_name repo_url retrieved_at valid_at "
        "available_at temporal_basis source source_url raw_record_id is_archived is_fork "
        "is_template stargazers_count forks_count open_issues_and_prs_count",
        booleans="is_archived is_fork is_template",
        integers="stargazers_count forks_count open_issues_and_prs_count"),
    "github_activity": _schema(
        "schema_version activity_id repo_id activity_kind source_object_id event_at actor_id "
        "actor_is_bot raw_record_id run_id source source_url retrieved_at valid_at available_at "
        "temporal_basis missing_fields_json", booleans="actor_is_bot"),
    "github_activity_details": _schema(
        "raw_record_id run_id repo_id retrieved_at entity_kind entity_id outcome_code entity_json"),
    "external_documents": _schema(
        "schema_version article_id raw_record_id run_id canonical_url original_url title article_kind "
        "published_at published_date published_precision updated_at content_status content_text "
        "content_sha256 content_available_at authors_json language_code technology_mentions_json "
        "repo_ids_json package_ids_json missing_fields_json source source_url retrieved_at valid_at "
        "available_at temporal_basis"),
    "external_relevance": _schema(
        "article_id raw_record_id target_package_id target_repo_id content_sha256 context_level "
        "decision match_method reason_code rule_version evidence_spans_json evidence_urls_json link_evidence_json"),
    "external_lineage": _schema(
        "article_id raw_record_id run_id capture_source source_url fetched_url "
        "discovery_lineage_complete metadata_json", booleans="discovery_lineage_complete"),
    "processing_audit": _schema(
        "processing_run_id processing_version item_type item_key raw_record_id payload_json"),
}
DATASET_KEYS = {
    "repositories": ("raw_record_id",),
    "github_activity": ("activity_id", "raw_record_id"),
    "github_activity_details": ("raw_record_id", "entity_kind"),
    "external_documents": ("article_id", "raw_record_id"),
    "external_relevance": ("article_id", "raw_record_id", "target_package_id", "rule_version"),
    "external_lineage": ("article_id", "raw_record_id"),
    "processing_audit": ("processing_run_id", "item_type", "item_key"),
}


def repository_row(record: GitHubRepository, raw_record_id: str | None = None) -> dict:
    """Keep the existing T-013 flat schema and observation grain."""
    record = validate_record(record)
    if not isinstance(record, GitHubRepository):
        raise TypeError("expected_canonical_repository")
    return {
        "schema_version": record.schema_version,
        **{key: getattr(record.identity, key) for key in
           ("repo_id", "github_repo_id", "repo_full_name", "repo_url")},
        **{key: getattr(record.provenance, key) for key in
           ("retrieved_at", "valid_at", "available_at", "temporal_basis", "source", "source_url")},
        "raw_record_id": raw_record_id or record.provenance.raw_record_ids[0],
        **{key: getattr(record, key) for key in
           ("is_archived", "is_fork", "is_template", "stargazers_count",
            "forks_count", "open_issues_and_prs_count")},
    }


class ProcessedDatasetWriter:
    """Single-writer upsert. Each Parquet replacement is atomic; the set is not a transaction."""

    def __init__(self, storage_paths: StoragePaths):
        self.paths = storage_paths
        self.parquet_storage = ParquetStorage()
        self.dataset_results: dict[str, DatasetWrite] = {}

    def _prepare(self, name: str, rows: list[dict]) -> tuple[pl.DataFrame, int, int, bool]:
        path = self.paths.processed_path(name)
        schema, keys = DATASET_SCHEMAS[name], DATASET_KEYS[name]
        incoming = pl.DataFrame(rows, schema=schema, strict=True)
        existing = self.parquet_storage.read(path)
        if (path.exists() or existing.width) and existing.schema != incoming.schema:
            raise ValueError(f"{name}: existing schema requires explicit migration")
        merged = {}
        for row in [*existing.to_dicts(), *incoming.to_dicts()]:
            if "schema_version" in row and row["schema_version"] != "0.1.0":
                raise ValueError(f"{name}: unsupported_schema_version")
            key = tuple(row[column] for column in keys)
            if any(value is None for value in key):
                raise ValueError(f"{name}: null_idempotency_key")
            previous = merged.get(key)
            if previous is not None and previous != row:
                raise ValueError(f"{name}: conflicting_idempotency_key")
            merged[key] = row
        old_keys = {tuple(row[column] for column in keys) for row in existing.to_dicts()}
        inserted = len(set(merged) - old_keys)
        order = ["retrieved_at", "github_repo_id", "raw_record_id"] if name == "repositories" else list(keys)
        frame = pl.DataFrame(list(merged.values()), schema=schema, strict=True).sort(order)
        return frame, inserted, existing.height, bool(inserted or frame.height != existing.height)

    def _persist(self, name: str, prepared: tuple[pl.DataFrame, int, int, bool]) -> int:
        frame, inserted, previous_count, changed = prepared
        info = DatasetWrite(path=str(self.paths.processed_path(name)), inserted_row_count=0,
                            total_row_count=previous_count, status="unchanged")
        self.dataset_results[name] = info
        if changed:
            try:
                self.parquet_storage.write(self.paths.processed_path(name), frame)
            except (OSError, StorageError, pl.exceptions.PolarsError):
                info.status = "failed"
                raise
            info.inserted_row_count, info.total_row_count, info.status = inserted, frame.height, "written"
        return inserted

    def upsert_repositories(self, rows: list[dict]) -> int:
        """Compatibility entrypoint for the existing RepositoryService projection."""
        return self._persist("repositories", self._prepare("repositories", rows)) if rows else 0

    def _project(self, result: "NormalizationPipelineResult", processing_run_id: str) -> dict[str, list[dict]]:
        rows = {name: [] for name in DATASET_SCHEMAS}

        def audit(kind: str, key: str, payload: Any, raw_id: str | None = None) -> None:
            rows["processing_audit"].append(dict(
                processing_run_id=processing_run_id, processing_version=result.processing_version,
                item_type=kind, item_key=key, raw_record_id=raw_id, payload_json=stable_json(payload)))

        for batch in result.canonical_normalization_results:
            for value in batch.records:
                record = validate_record(value)
                provenance = record.provenance.model_dump(mode="json")
                raw_ids = provenance.pop("raw_record_ids")
                missing = provenance.pop("missing_fields")
                for raw_id in raw_ids:
                    if record.record_type == "github_repository":
                        reasons = repository_exclusion_reasons(record)
                        if reasons:
                            audit("repository_exclusion", raw_id,
                                  dict(repo_id=record.identity.repo_id, reasons=list(reasons)), raw_id)
                        else:
                            rows["repositories"].append(repository_row(record, raw_id))
                    elif record.record_type == "repository_activity":
                        row = record.model_dump(mode="json", exclude={"record_type", "provenance"})
                        rows["github_activity"].append(dict(
                            **row, **provenance, raw_record_id=raw_id, run_id=batch.run_id,
                            missing_fields_json=stable_json(missing)))
                    elif record.record_type == "article":
                        row = record.model_dump(mode="json", exclude={"record_type", "provenance"})
                        for key in ("authors", "technology_mentions", "repo_ids", "package_ids"):
                            row[key + "_json"] = stable_json(row.pop(key))
                        rows["external_documents"].append(dict(
                            **row, **provenance, raw_record_id=raw_id, run_id=batch.run_id,
                            missing_fields_json=stable_json(missing)))
                    else:
                        raise ValueError("unsupported_processed_record_type")

        kinds = {"GitHubCommit": ("commit", "commit_sha"), "GitHubIssue": ("issue", "issue_id"),
                 "GitHubPullRequest": ("pull_request", "pr_id"),
                 "GitHubContributorObservation": ("contributor", "contributor_github_id"),
                 "GitHubRelease": ("release", "release_id")}
        for observation in result.internal_activity_entities:
            kind, identifier = kinds[type(observation.entity).__name__]
            rows["github_activity_details"].append(dict(
                raw_record_id=observation.raw_record_id, run_id=observation.run_id,
                repo_id=observation.entity.repo_id, retrieved_at=observation.retrieved_at,
                entity_kind=kind, entity_id=getattr(observation.entity, identifier) or observation.raw_record_id,
                outcome_code=observation.outcome_code,
                entity_json=stable_json(observation.entity.model_dump(mode="json"))))
        for assessment in result.relevance_assessments:
            row = assessment.model_dump(mode="json", exclude={"raw_record_ids"})
            for key in ("evidence_spans", "evidence_urls", "link_evidence"):
                row[key + "_json"] = stable_json(row.pop(key))
            for raw_id in assessment.raw_record_ids:
                rows["external_relevance"].append(dict(**row, raw_record_id=raw_id))
        for reference in result.source_lineage_references:
            row = reference.model_dump(mode="json")
            row["metadata_json"] = stable_json(row.pop("metadata"))
            rows["external_lineage"].append(row)
        for kind, groups in (
            ("repository_group", result.deduplicated_repository_view),
            ("activity_group", result.deduplicated_activity_view),
            ("document_group", result.deduplicated_document_view),
        ):
            for group in groups:
                audit(kind, group.logical_id, group.model_dump(mode="json"))
        for duplicate in result.content_duplicates:
            audit("content_duplicate", duplicate.content_sha256, duplicate.model_dump(mode="json"))
        for index, error in enumerate(result.processing_errors):
            audit("processing_error", str(index), error.model_dump(mode="json"), error.raw_record_id)
        for raw_id, diagnostic in sorted(result.temporal_diagnostics.items()):
            audit("temporal_diagnostic", raw_id, diagnostic, raw_id)
        return rows

    @staticmethod
    def _check_observations(audit: pl.DataFrame) -> None:
        """Cross-run state fingerprints catch changes omitted by flat entity projections."""
        from reposeer.pipelines.normalization import DocumentGroup, EntityGroup

        observations = {}
        for row in audit.iter_rows(named=True):
            if row["item_type"] not in {"repository_group", "activity_group", "document_group"}:
                continue
            model = DocumentGroup if row["item_type"] == "document_group" else EntityGroup
            group = model.model_validate_json(row["payload_json"])
            for reference in group.observations:
                observation = reference.model_dump(mode="json")
                raw_id = reference.raw_record_id
                if raw_id in observations and observations[raw_id] != observation:
                    raise ValueError("conflicting_raw_observation_state")
                observations[raw_id] = observation

    def write(self, result: "NormalizationPipelineResult", *, processing_run_id: str) -> dict[str, int]:
        from reposeer.pipelines.normalization import NormalizationPipelineResult

        TypeAdapter(UuidText).validate_python(processing_run_id, strict=True)
        result = NormalizationPipelineResult.model_validate(result.model_dump())
        self.dataset_results = {}
        rows = self._project(result, processing_run_id)
        prepared = {}
        for name in DATASET_SCHEMAS:
            self.dataset_results[name] = DatasetWrite(
                path=str(self.paths.processed_path(name)), inserted_row_count=0,
                total_row_count=None, status="pending")
            try:
                prepared[name] = self._prepare(name, rows[name])
            except (ValueError, TypeError, OSError, StorageError, pl.exceptions.PolarsError):
                self.dataset_results[name].status = "failed"
                raise
            self.dataset_results[name].total_row_count = prepared[name][2]
        try:
            self._check_observations(prepared["processing_audit"][0])
        except ValueError:
            self.dataset_results["processing_audit"].status = "failed"
            raise
        return {name: self._persist(name, prepared[name]) for name in DATASET_SCHEMAS}
