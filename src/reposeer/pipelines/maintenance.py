"""Deterministic D09 label and official status evaluation pipeline."""
from __future__ import annotations

from typing import Literal, Sequence

import polars as pl

from reposeer.contracts.analysis import OfficialStatus
from reposeer.contracts.identity import add_calendar_months
from reposeer.contracts.models import (
    PackageId,
    PackageRelease,
    RepoId,
    RepositoryActivity,
    UtcTimestamp,
)
from reposeer.maintenance.labels import (
    HISTORICAL_LABEL_SCHEMA,
    HistoricalLabelRecord,
    labels_to_polars,
    polars_to_labels,
)


class MaintenancePipeline:
    """Canonical D09 label pipeline for historical ground truth and official status."""

    LABEL_POLICY_VERSION: str = "0.1.0"

    def __init__(self, policy_version: str = "0.1.0") -> None:
        self.policy_version = policy_version

    labels_to_polars = staticmethod(labels_to_polars)
    polars_to_labels = staticmethod(polars_to_labels)

    def evaluate_as_of_status(
        self,
        *,
        cutoff_at: UtcTimestamp,
        official_events: Sequence[OfficialStatus] = (),
    ) -> tuple[
        Literal["already_abandoned", "scheduled_cessation", "status_unverified"],
        OfficialStatus | None,
        bool | None,
    ]:
        """Evaluate project official maintenance status as of cutoff_at."""
        known_events: list[OfficialStatus] = []
        for ev in official_events:
            ver_time = getattr(ev, "available_at", None) or ev.verified_at
            if ev.announced_at > cutoff_at or ver_time > cutoff_at:
                continue
            if ev.revoked_at is not None and ev.revoked_at <= cutoff_at:
                continue
            known_events.append(ev)

        # 1. Past confirmed cessation
        for ev in known_events:
            if ev.cessation_confirmed and ev.effective_at <= cutoff_at:
                return ("already_abandoned", ev, None)

        # 2. Future scheduled cessation
        horizon_end = add_calendar_months(cutoff_at, 12)
        for ev in known_events:
            if ev.effective_at > cutoff_at:
                within_horizon = ev.effective_at <= horizon_end
                return ("scheduled_cessation", ev, within_horizon)

        return ("status_unverified", None, None)

    def evaluate_horizon_label(
        self,
        *,
        package_id: PackageId,
        repo_id: RepoId,
        cutoff_at: UtcTimestamp,
        observation_end_at: UtcTimestamp,
        official_events: Sequence[OfficialStatus] = (),
        horizon_releases: Sequence[PackageRelease] = (),
        horizon_activities: Sequence[RepositoryActivity] = (),
        horizon_coverage_complete: bool = True,
    ) -> HistoricalLabelRecord:
        """Evaluate 12-month horizon label and risk-set eligibility."""
        horizon_end_at = add_calendar_months(cutoff_at, 12)

        as_of_status, as_of_event, within_horizon = self.evaluate_as_of_status(
            cutoff_at=cutoff_at, official_events=official_events
        )

        # Factual cessation at or before cutoff (even if discovered post-cutoff)
        factual_abandoned_event: OfficialStatus | None = None
        if as_of_status == "already_abandoned":
            factual_abandoned_event = as_of_event
        else:
            for ev in official_events:
                if ev.cessation_confirmed and ev.effective_at <= cutoff_at:
                    if ev.revoked_at is not None and ev.revoked_at <= cutoff_at:
                        continue
                    factual_abandoned_event = ev
                    break

        if factual_abandoned_event is not None:
            return HistoricalLabelRecord(
                package_id=package_id,
                repo_id=repo_id,
                repo_group_id=repo_id,
                cutoff_at=cutoff_at,
                horizon_end_at=horizon_end_at,
                as_of_status="already_abandoned",
                official_status=factual_abandoned_event,
                within_12m_horizon=None,
                in_risk_set=False,
                label=None,
                label_status="excluded_known_status",
                censor_reason="already_abandoned_at_cutoff",
                label_available_at=None,
                event_effective_at=factual_abandoned_event.effective_at,
                evidence_ids=list(factual_abandoned_event.evidence_ids),
                label_policy_version=self.LABEL_POLICY_VERSION,
            )

        if as_of_status == "scheduled_cessation":
            return HistoricalLabelRecord(
                package_id=package_id,
                repo_id=repo_id,
                repo_group_id=repo_id,
                cutoff_at=cutoff_at,
                horizon_end_at=horizon_end_at,
                as_of_status="scheduled_cessation",
                official_status=as_of_event,
                within_12m_horizon=within_horizon,
                in_risk_set=False,
                label=None,
                label_status="excluded_known_status",
                censor_reason="scheduled_cessation_at_cutoff",
                label_available_at=None,
                event_effective_at=as_of_event.effective_at if as_of_event else None,
                evidence_ids=list(as_of_event.evidence_ids) if as_of_event else [],
                label_policy_version=self.LABEL_POLICY_VERSION,
            )

        # Positive (label = 1): confirmed cessation taking effect in horizon
        positive_event: OfficialStatus | None = None
        for ev in official_events:
            if not ev.cessation_confirmed:
                continue
            if not (cutoff_at < ev.effective_at <= horizon_end_at):
                continue
            ver_time = getattr(ev, "available_at", None) or ev.verified_at
            if ver_time > observation_end_at:
                continue
            if ev.revoked_at is not None and ev.revoked_at <= observation_end_at:
                continue
            positive_event = ev
            break

        if positive_event is not None:
            label_avail = max(positive_event.effective_at, positive_event.verified_at)
            return HistoricalLabelRecord(
                package_id=package_id,
                repo_id=repo_id,
                repo_group_id=repo_id,
                cutoff_at=cutoff_at,
                horizon_end_at=horizon_end_at,
                as_of_status=as_of_status,
                official_status=positive_event,
                within_12m_horizon=True,
                in_risk_set=True,
                label=1,
                label_status="positive",
                censor_reason=None,
                label_available_at=label_avail,
                event_effective_at=positive_event.effective_at,
                evidence_ids=list(positive_event.evidence_ids),
                label_policy_version=self.LABEL_POLICY_VERSION,
            )

        # Follow-up maturity and coverage completeness checks
        if observation_end_at < horizon_end_at:
            return HistoricalLabelRecord(
                package_id=package_id,
                repo_id=repo_id,
                repo_group_id=repo_id,
                cutoff_at=cutoff_at,
                horizon_end_at=horizon_end_at,
                as_of_status=as_of_status,
                official_status=None,
                within_12m_horizon=None,
                in_risk_set=True,
                label=None,
                label_status="unknown",
                censor_reason="insufficient_followup",
                label_available_at=None,
                event_effective_at=None,
                evidence_ids=[],
                label_policy_version=self.LABEL_POLICY_VERSION,
            )

        if not horizon_coverage_complete:
            return HistoricalLabelRecord(
                package_id=package_id,
                repo_id=repo_id,
                repo_group_id=repo_id,
                cutoff_at=cutoff_at,
                horizon_end_at=horizon_end_at,
                as_of_status=as_of_status,
                official_status=None,
                within_12m_horizon=None,
                in_risk_set=True,
                label=None,
                label_status="unknown",
                censor_reason="incomplete_coverage",
                label_available_at=None,
                event_effective_at=None,
                evidence_ids=[],
                label_policy_version=self.LABEL_POLICY_VERSION,
            )

        # Negative (label = 0): verified maintenance during horizon
        has_verified_maintenance = False

        for rel in horizon_releases:
            rel_time = getattr(rel, "published_at", None) or getattr(rel, "uploaded_at", None)
            if rel_time is None or not (cutoff_at < rel_time <= horizon_end_at):
                continue
            prov = getattr(rel, "provenance", None)
            rel_avail = (
                getattr(prov, "available_at", None) if prov else getattr(rel, "available_at", None)
            )
            if rel_avail is not None and rel_avail <= observation_end_at:
                has_verified_maintenance = True
                break

        if not has_verified_maintenance:
            for act in horizon_activities:
                if not (cutoff_at < act.event_at <= horizon_end_at):
                    continue
                prov = getattr(act, "provenance", None)
                act_avail = (
                    getattr(prov, "available_at", None) if prov else getattr(act, "available_at", None)
                )
                if act_avail is None or act_avail > observation_end_at:
                    continue
                if act.activity_kind in ("commit", "pr_merged", "issue_closed", "release"):
                    if act.actor_is_bot is not True:
                        has_verified_maintenance = True
                        break

        if has_verified_maintenance:
            return HistoricalLabelRecord(
                package_id=package_id,
                repo_id=repo_id,
                repo_group_id=repo_id,
                cutoff_at=cutoff_at,
                horizon_end_at=horizon_end_at,
                as_of_status=as_of_status,
                official_status=None,
                within_12m_horizon=None,
                in_risk_set=True,
                label=0,
                label_status="negative",
                censor_reason=None,
                label_available_at=horizon_end_at,
                event_effective_at=None,
                evidence_ids=[],
                label_policy_version=self.LABEL_POLICY_VERSION,
            )

        # Silent / Unverified: 0 verified maintenance events
        return HistoricalLabelRecord(
            package_id=package_id,
            repo_id=repo_id,
            repo_group_id=repo_id,
            cutoff_at=cutoff_at,
            horizon_end_at=horizon_end_at,
            as_of_status=as_of_status,
            official_status=None,
            within_12m_horizon=None,
            in_risk_set=True,
            label=None,
            label_status="unknown",
            censor_reason="silent_unverified",
            label_available_at=None,
            event_effective_at=None,
            evidence_ids=[],
            label_policy_version=self.LABEL_POLICY_VERSION,
        )
