"""Feature extraction modules for repository, package, and activity metrics."""
from reposeer.features.activity import ActivityFeatureResult, ActivityFeatures
from reposeer.features.builder import FEATURE_COLUMNS, FeatureBuilder
from reposeer.features.package import PackageFeatureResult, PackageFeatures
from reposeer.features.repository import (
    FORBIDDEN_COUNTER_FIELDS,
    RepositoryFeatures,
    assert_no_counter_leakage,
    compute_horizon_end_at,
    compute_lookback_boundaries,
    group_missing_reason,
    index_coverage,
    validate_snapshot_grain,
)

__all__ = [
    "FEATURE_COLUMNS",
    "FORBIDDEN_COUNTER_FIELDS",
    "ActivityFeatureResult",
    "ActivityFeatures",
    "FeatureBuilder",
    "PackageFeatureResult",
    "PackageFeatures",
    "RepositoryFeatures",
    "assert_no_counter_leakage",
    "compute_horizon_end_at",
    "compute_lookback_boundaries",
    "group_missing_reason",
    "index_coverage",
    "validate_snapshot_grain",
]
