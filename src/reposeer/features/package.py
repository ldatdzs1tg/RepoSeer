"""Package feature calculator enforcing temporal validity, boundary rules, and coverage."""
from __future__ import annotations

from datetime import datetime
from typing import Mapping, NamedTuple, Sequence

from reposeer.contracts.models import (
    Coverage,
    DataGroup,
    MissingField,
    PackageId,
    PackageRelease,
    UtcTimestamp,
)
from reposeer.features.repository import (
    compute_lookback_boundaries,
    group_missing_reason,
    index_coverage,
)


class PackageFeatureResult(NamedTuple):
    features: dict[str, int | None]
    missing_fields: list[MissingField]


class PackageFeatures:
    """Calculates historical package release features up to cutoff_at."""

    RELEASE_FEATURE_NAMES = (
        "package_age_days",
        "releases_total",
        "days_since_latest_release",
        "release_count_3m",
        "release_count_6m",
        "release_count_12m",
    )

    @classmethod
    def compute(
        cls,
        package_id: PackageId,
        cutoff_at: UtcTimestamp,
        releases: Sequence[PackageRelease],
        coverage_map: Mapping[DataGroup, Coverage] | Sequence[Coverage],
    ) -> PackageFeatureResult:
        """Compute 6 package release features under temporal and coverage constraints."""
        if not isinstance(coverage_map, Mapping):
            coverage_map = index_coverage(coverage_map, cutoff_at)

        # Validate temporal invariants and identity on each release
        for release in releases:
            if release.package_id != package_id:
                raise ValueError(
                    f"Release package_id mismatch: expected {package_id}, got {release.package_id}"
                )
            rel_time = getattr(release, "uploaded_at", None) or getattr(release, "published_at", None)
            if rel_time is None:
                raise ValueError(f"Release {release.version} has no uploaded_at/published_at timestamp")
            if rel_time > cutoff_at:
                raise ValueError(
                    f"Release {release.version} timestamp {rel_time} is after cutoff {cutoff_at}"
                )
            if release.provenance.valid_at > cutoff_at:
                raise ValueError(
                    f"Release {release.version} provenance.valid_at {release.provenance.valid_at} > cutoff {cutoff_at}"
                )
            if release.provenance.available_at is None:
                raise ValueError(
                    f"Release {release.version} provenance.available_at is None (unverified availability)"
                )
            if release.provenance.available_at > cutoff_at:
                raise ValueError(
                    f"Release {release.version} provenance.available_at {release.provenance.available_at} > cutoff {cutoff_at}"
                )
            if getattr(release, "yanked_status_at", None) is not None and release.yanked_status_at > cutoff_at:
                raise ValueError(
                    f"Release {release.version} yanked_status_at {release.yanked_status_at} > cutoff {cutoff_at}"
                )

        is_complete, missing_reason, detail = group_missing_reason(coverage_map, "releases")
        if not is_complete:
            reason = missing_reason or "unavailable"
            features = {name: None for name in cls.RELEASE_FEATURE_NAMES}
            missing_fields = [
                MissingField(field_path=f"features.{name}", reason=reason, detail=detail)
                for name in cls.RELEASE_FEATURE_NAMES
            ]
            return PackageFeatureResult(features=features, missing_fields=missing_fields)

        # Deduplicate valid releases up to cutoff by version
        unique_by_version: dict[str, PackageRelease] = {}
        for release in releases:
            v = release.version
            if v not in unique_by_version:
                unique_by_version[v] = release
            else:
                existing_time = getattr(unique_by_version[v], "uploaded_at", None) or getattr(unique_by_version[v], "published_at", "")
                cur_time = getattr(release, "uploaded_at", None) or getattr(release, "published_at", "")
                if cur_time > existing_time:
                    unique_by_version[v] = release

        unique_releases = list(unique_by_version.values())
        releases_total = len(unique_releases)

        bounds = compute_lookback_boundaries(cutoff_at)
        t_3m = bounds["3m"]
        t_6m = bounds["6m"]
        t_12m = bounds["12m"]

        rel_timestamps = [
            getattr(r, "uploaded_at", None) or getattr(r, "published_at", "")
            for r in unique_releases
        ]

        release_count_3m = sum(1 for ts in rel_timestamps if t_3m < ts <= cutoff_at)
        release_count_6m = sum(1 for ts in rel_timestamps if t_6m < ts <= cutoff_at)
        release_count_12m = sum(1 for ts in rel_timestamps if t_12m < ts <= cutoff_at)

        missing_fields: list[MissingField] = []
        if releases_total > 0:
            cutoff_dt = datetime.fromisoformat(cutoff_at.replace("Z", "+00:00"))
            earliest_dt = datetime.fromisoformat(min(rel_timestamps).replace("Z", "+00:00"))
            latest_dt = datetime.fromisoformat(max(rel_timestamps).replace("Z", "+00:00"))
            package_age_days: int | None = max(0, (cutoff_dt - earliest_dt).days)
            days_since_latest_release: int | None = max(0, (cutoff_dt - latest_dt).days)
        else:
            package_age_days = None
            days_since_latest_release = None
            missing_fields.extend([
                MissingField(
                    field_path="features.package_age_days",
                    reason="not_applicable",
                    detail="no package releases observed on or before cutoff_at",
                ),
                MissingField(
                    field_path="features.days_since_latest_release",
                    reason="not_applicable",
                    detail="no package releases observed on or before cutoff_at",
                ),
            ])

        features = {
            "package_age_days": package_age_days,
            "releases_total": releases_total,
            "days_since_latest_release": days_since_latest_release,
            "release_count_3m": release_count_3m,
            "release_count_6m": release_count_6m,
            "release_count_12m": release_count_12m,
        }

        return PackageFeatureResult(features=features, missing_fields=missing_fields)
