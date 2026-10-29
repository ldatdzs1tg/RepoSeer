"""Historical label records and schema for D09 evaluation."""
from __future__ import annotations

import json
from typing import Literal, Sequence

import polars as pl
from pydantic import Field, model_validator

from reposeer.contracts.analysis import OfficialStatus
from reposeer.contracts.models import (
    ContractModel,
    PackageId,
    RepoId,
    UtcTimestamp,
)

HISTORICAL_LABEL_SCHEMA: dict[str, pl.DataType] = {
    "package_id": pl.String,
    "repo_id": pl.String,
    "repo_group_id": pl.String,
    "cutoff_at": pl.String,
    "horizon_end_at": pl.String,
    "as_of_status": pl.String,
    "within_12m_horizon": pl.Boolean,
    "in_risk_set": pl.Boolean,
    "label": pl.Int64,
    "label_status": pl.String,
    "censor_reason": pl.String,
    "label_available_at": pl.String,
    "event_effective_at": pl.String,
    "evidence_ids_json": pl.String,
    "official_status_json": pl.String,
    "label_policy_version": pl.String,
}


class HistoricalLabelRecord(ContractModel):
    """Strongly typed D09 historical label record."""

    package_id: PackageId
    repo_id: RepoId
    repo_group_id: RepoId
    cutoff_at: UtcTimestamp
    horizon_end_at: UtcTimestamp
    as_of_status: Literal["already_abandoned", "scheduled_cessation", "status_unverified"]
    official_status: OfficialStatus | None = None
    within_12m_horizon: bool | None = None
    in_risk_set: bool
    label: Literal[0, 1] | None
    label_status: Literal["positive", "negative", "unknown", "excluded_known_status"]
    censor_reason: (
        Literal[
            "already_abandoned_at_cutoff",
            "scheduled_cessation_at_cutoff",
            "insufficient_followup",
            "incomplete_coverage",
            "silent_unverified",
            "conflicting_evidence",
        ]
        | None
    ) = None
    label_available_at: UtcTimestamp | None = None
    event_effective_at: UtcTimestamp | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    label_policy_version: str = "0.1.0"

    @model_validator(mode="after")
    def check_invariants(self) -> HistoricalLabelRecord:
        if self.repo_group_id != self.repo_id:
            raise ValueError("repo_group_id must equal repo_id")
        if not self.in_risk_set:
            if self.label is not None:
                raise ValueError("excluded record cannot have a label")
            if self.label_status != "excluded_known_status":
                raise ValueError("excluded record must have label_status='excluded_known_status'")
            if self.censor_reason not in (
                "already_abandoned_at_cutoff",
                "scheduled_cessation_at_cutoff",
            ):
                raise ValueError(f"excluded record invalid censor_reason: {self.censor_reason}")
        elif self.label == 1:
            if self.label_status != "positive":
                raise ValueError("label 1 requires label_status='positive'")
            if self.censor_reason is not None:
                raise ValueError("positive label cannot have a censor_reason")
        elif self.label == 0:
            if self.label_status != "negative":
                raise ValueError("label 0 requires label_status='negative'")
            if self.censor_reason is not None:
                raise ValueError("negative label cannot have a censor_reason")
        else:
            if self.label_status != "unknown":
                raise ValueError("unlabeled in-risk record requires label_status='unknown'")
            if self.censor_reason is None:
                raise ValueError("unknown label requires a censor_reason")
        return self


def labels_to_polars(records: Sequence[HistoricalLabelRecord]) -> pl.DataFrame:
    """Convert sequence of HistoricalLabelRecord to tabular Polars DataFrame."""
    if not records:
        return pl.DataFrame(schema=HISTORICAL_LABEL_SCHEMA)

    rows: list[dict] = []
    for r in records:
        rows.append(
            {
                "package_id": r.package_id,
                "repo_id": r.repo_id,
                "repo_group_id": r.repo_group_id,
                "cutoff_at": r.cutoff_at,
                "horizon_end_at": r.horizon_end_at,
                "as_of_status": r.as_of_status,
                "within_12m_horizon": r.within_12m_horizon,
                "in_risk_set": r.in_risk_set,
                "label": r.label,
                "label_status": r.label_status,
                "censor_reason": r.censor_reason,
                "label_available_at": r.label_available_at,
                "event_effective_at": r.event_effective_at,
                "evidence_ids_json": json.dumps(r.evidence_ids),
                "official_status_json": (
                    json.dumps(r.official_status.model_dump(mode="json"))
                    if r.official_status is not None
                    else None
                ),
                "label_policy_version": r.label_policy_version,
            }
        )
    return pl.DataFrame(rows, schema=HISTORICAL_LABEL_SCHEMA)


def polars_to_labels(df: pl.DataFrame) -> list[HistoricalLabelRecord]:
    """Reconstruct HistoricalLabelRecord list from Polars DataFrame."""
    if df.is_empty():
        return []
    records: list[HistoricalLabelRecord] = []
    for row in df.iter_rows(named=True):
        off_json = row.get("official_status_json")
        off_status = (
            OfficialStatus.model_validate(json.loads(off_json)) if off_json is not None else None
        )
        ev_ids = json.loads(row["evidence_ids_json"]) if row.get("evidence_ids_json") else []
        records.append(
            HistoricalLabelRecord(
                package_id=row["package_id"],
                repo_id=row["repo_id"],
                repo_group_id=row["repo_group_id"],
                cutoff_at=row["cutoff_at"],
                horizon_end_at=row["horizon_end_at"],
                as_of_status=row["as_of_status"],
                official_status=off_status,
                within_12m_horizon=row["within_12m_horizon"],
                in_risk_set=row["in_risk_set"],
                label=row["label"],
                label_status=row["label_status"],
                censor_reason=row["censor_reason"],
                label_available_at=row["label_available_at"],
                event_effective_at=row["event_effective_at"],
                evidence_ids=ev_ids,
                label_policy_version=row["label_policy_version"],
            )
        )
    return records
