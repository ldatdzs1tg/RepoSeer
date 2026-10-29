"""Activity feature calculator for repository events across temporal windows."""
from __future__ import annotations

from typing import Mapping, NamedTuple, Sequence

from reposeer.contracts.models import (
    Coverage,
    DataGroup,
    MissingField,
    RepoId,
    RepositoryActivity,
    UtcTimestamp,
)
from reposeer.features.repository import (
    compute_lookback_boundaries,
    group_missing_reason,
    index_coverage,
)


class ActivityFeatureResult(NamedTuple):
    features: dict[str, int | None]
    missing_fields: list[MissingField]


class ActivityFeatures:
    """Calculates repository activity features (commits, contributors, issues, pull requests)."""

    COMMIT_FEATURE_NAMES = (
        "commit_count_3m",
        "commit_count_6m",
        "commit_count_12m",
    )
    ISSUE_FEATURE_NAMES = (
        "issue_opened_count_12m",
        "issue_closed_count_12m",
    )
    PR_FEATURE_NAMES = (
        "pr_opened_count_12m",
        "pr_merged_count_12m",
    )

    @classmethod
    def compute(
        cls,
        repo_id: RepoId,
        cutoff_at: UtcTimestamp,
        activities: Sequence[RepositoryActivity],
        coverage_map: Mapping[DataGroup, Coverage] | Sequence[Coverage],
    ) -> ActivityFeatureResult:
        """Compute 8 activity features across commits, contributors, issues, and pull requests."""
        if not isinstance(coverage_map, Mapping):
            coverage_map = index_coverage(coverage_map, cutoff_at)

        # Validate temporal invariants and identity on each activity
        for act in activities:
            if act.repo_id != repo_id:
                raise ValueError(
                    f"Activity repo_id mismatch: expected {repo_id}, got {act.repo_id}"
                )
            if act.event_at > cutoff_at:
                raise ValueError(
                    f"Activity {act.activity_id} event_at {act.event_at} > cutoff {cutoff_at}"
                )
            if act.provenance.valid_at > cutoff_at:
                raise ValueError(
                    f"Activity {act.activity_id} provenance.valid_at {act.provenance.valid_at} > cutoff {cutoff_at}"
                )
            if act.provenance.available_at is None:
                raise ValueError(
                    f"Activity {act.activity_id} provenance.available_at is None (unverified availability)"
                )
            if act.provenance.available_at > cutoff_at:
                raise ValueError(
                    f"Activity {act.activity_id} provenance.available_at {act.provenance.available_at} > cutoff {cutoff_at}"
                )

        # Deduplicate activities by activity_id
        unique_activities: list[RepositoryActivity] = []
        seen_ids: set[str] = set()
        for act in activities:
            if act.activity_id not in seen_ids:
                seen_ids.add(act.activity_id)
                unique_activities.append(act)

        bounds = compute_lookback_boundaries(cutoff_at)
        t_3m = bounds["3m"]
        t_6m = bounds["6m"]
        t_12m = bounds["12m"]

        features: dict[str, int | None] = {}
        missing_fields: list[MissingField] = []

        # 1. Commits group
        commits_complete, commits_reason, commits_detail = group_missing_reason(coverage_map, "commits")
        if commits_complete:
            commit_events = [a for a in unique_activities if a.activity_kind == "commit"]
            features["commit_count_3m"] = sum(1 for a in commit_events if t_3m < a.event_at <= cutoff_at)
            features["commit_count_6m"] = sum(1 for a in commit_events if t_6m < a.event_at <= cutoff_at)
            features["commit_count_12m"] = sum(1 for a in commit_events if t_12m < a.event_at <= cutoff_at)
        else:
            reason = commits_reason or "unavailable"
            for name in cls.COMMIT_FEATURE_NAMES:
                features[name] = None
                missing_fields.append(
                    MissingField(field_path=f"features.{name}", reason=reason, detail=commits_detail)
                )

        # 2. Contributors group
        contrib_complete, contrib_reason, contrib_detail = group_missing_reason(coverage_map, "contributors")
        if contrib_complete:
            commits_12m = [
                a for a in unique_activities
                if a.activity_kind == "commit" and t_12m < a.event_at <= cutoff_at
            ]
            is_ambiguous = False
            human_actor_ids: set[str] = set()
            for c in commits_12m:
                if c.actor_is_bot is True:
                    continue
                if c.actor_is_bot is None or (c.actor_is_bot is False and c.actor_id is None):
                    is_ambiguous = True
                    break
                human_actor_ids.add(c.actor_id)

            if is_ambiguous:
                features["contributor_count_12m"] = None
                missing_fields.append(
                    MissingField(
                        field_path="features.contributor_count_12m",
                        reason="ambiguous",
                        detail="commit actor identity or bot classification missing in 12m window",
                    )
                )
            else:
                features["contributor_count_12m"] = len(human_actor_ids)
        else:
            features["contributor_count_12m"] = None
            missing_fields.append(
                MissingField(
                    field_path="features.contributor_count_12m",
                    reason=contrib_reason or "unavailable",
                    detail=contrib_detail,
                )
            )

        # 3. Issues group
        issues_complete, issues_reason, issues_detail = group_missing_reason(coverage_map, "issues")
        if issues_complete:
            features["issue_opened_count_12m"] = sum(
                1 for a in unique_activities
                if a.activity_kind == "issue_opened" and t_12m < a.event_at <= cutoff_at
            )
            features["issue_closed_count_12m"] = sum(
                1 for a in unique_activities
                if a.activity_kind == "issue_closed" and t_12m < a.event_at <= cutoff_at
            )
        else:
            reason = issues_reason or "unavailable"
            for name in cls.ISSUE_FEATURE_NAMES:
                features[name] = None
                missing_fields.append(
                    MissingField(field_path=f"features.{name}", reason=reason, detail=issues_detail)
                )

        # 4. Pull Requests group
        prs_complete, prs_reason, prs_detail = group_missing_reason(coverage_map, "pull_requests")
        if prs_complete:
            features["pr_opened_count_12m"] = sum(
                1 for a in unique_activities
                if a.activity_kind == "pr_opened" and t_12m < a.event_at <= cutoff_at
            )
            features["pr_merged_count_12m"] = sum(
                1 for a in unique_activities
                if a.activity_kind == "pr_merged" and t_12m < a.event_at <= cutoff_at
            )
        else:
            reason = prs_reason or "unavailable"
            for name in cls.PR_FEATURE_NAMES:
                features[name] = None
                missing_fields.append(
                    MissingField(field_path=f"features.{name}", reason=reason, detail=prs_detail)
                )

        return ActivityFeatureResult(features=features, missing_fields=missing_fields)
