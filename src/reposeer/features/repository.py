"""Repository feature extraction, temporal window boundaries, and coverage gating."""
from __future__ import annotations

from typing import Mapping, Sequence

from reposeer.contracts.identity import add_calendar_months
from reposeer.contracts.models import (
    Coverage,
    DataGroup,
    MissingReason,
    RepoId,
    RepoIdentity,
    UtcTimestamp,
    unique,
)

FORBIDDEN_COUNTER_FIELDS = frozenset({
    "stargazers_count",
    "forks_count",
    "open_issues_and_prs_count",
    "stars",
    "forks",
    "open_issues_count",
})


def validate_snapshot_grain(
    repo_id: RepoId | str,
    repo_group_id: RepoId | str,
) -> None:
    """Ensure repository grain alignment."""
    if repo_group_id != repo_id:
        raise ValueError(f"repo_group_id ({repo_group_id}) must equal repo_id ({repo_id}) in v0.1")


def compute_horizon_end_at(cutoff_at: UtcTimestamp, horizon_months: int = 12) -> UtcTimestamp:
    """Compute horizon end timestamp by adding calendar months."""
    return add_calendar_months(cutoff_at, horizon_months)


def compute_lookback_boundaries(cutoff_at: UtcTimestamp) -> dict[str, UtcTimestamp]:
    """Compute half-open lookback start boundaries (cutoff - W) in UTC."""
    return {
        "3m": add_calendar_months(cutoff_at, -3),
        "6m": add_calendar_months(cutoff_at, -6),
        "12m": add_calendar_months(cutoff_at, -12),
    }


def index_coverage(
    coverage: Sequence[Coverage],
    cutoff_at: UtcTimestamp,
) -> dict[DataGroup, Coverage]:
    """Index coverage entries by data_group and verify time ranges do not leak beyond cutoff."""
    unique([cov.data_group for cov in coverage], "coverage.data_group")
    coverage_map: dict[DataGroup, Coverage] = {}
    for cov in coverage:
        if cov.time_range is not None and cov.time_range.end_at > cutoff_at:
            raise ValueError(
                f"snapshot coverage for group '{cov.data_group}' extends past cutoff "
                f"({cov.time_range.end_at} > {cutoff_at})"
            )
        coverage_map[cov.data_group] = cov
    return coverage_map


def group_missing_reason(
    coverage_map: Mapping[DataGroup, Coverage],
    group: DataGroup,
) -> tuple[bool, MissingReason | None, str]:
    """Check coverage status for a group.

    Returns:
        (is_complete, missing_reason, detail)
        where is_complete is True ONLY when group exists and status == 'complete'.
    """
    if group not in coverage_map:
        return (False, "not_collected", f"Data group '{group}' not present in coverage")
    cov = coverage_map[group]
    if cov.status == "complete":
        return (True, None, f"Data group '{group}' coverage is complete")
    reason = cov.missing_reason or "unavailable"
    detail = f"Data group '{group}' coverage status is '{cov.status}'"
    return (False, reason, detail)


def assert_no_counter_leakage(data: Mapping[str, object] | Sequence[str]) -> None:
    """Enforce anti-leakage rule: current repository counters cannot be used as features."""
    keys = set(data.keys()) if isinstance(data, Mapping) else set(data)
    forbidden = keys & FORBIDDEN_COUNTER_FIELDS
    if forbidden:
        raise ValueError(
            f"Anti-leakage violation: current repository counters cannot be exposed as historical features: {sorted(forbidden)}"
        )


class RepositoryFeatures:
    """Repository feature helpers for grain, coverage, lookback windows, and anti-leakage."""

    validate_grain = staticmethod(validate_snapshot_grain)
    compute_horizon_end_at = staticmethod(compute_horizon_end_at)
    compute_lookback_boundaries = staticmethod(compute_lookback_boundaries)
    index_coverage = staticmethod(index_coverage)
    group_missing_reason = staticmethod(group_missing_reason)
    assert_no_counter_leakage = staticmethod(assert_no_counter_leakage)
