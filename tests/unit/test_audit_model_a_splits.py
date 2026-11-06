from scripts.audit_model_a_splits import audit_splits

from reposeer.contracts.identity import add_calendar_months

TIMES = {
    "fold_fit_at": "2022-12-31T23:59:59.999Z",
    "validation_start_at": "2023-01-01T00:00:00.000Z",
    "selection_freeze_at": "2024-09-30T23:59:59.999Z",
    "test_start_at": "2024-10-01T00:00:00.000Z",
    "test_end_at": "2025-01-01T00:00:00.000Z",
    "evaluation_as_of": "2026-01-01T00:00:00.000Z",
}


def row(name, cutoff, group, *, label=0, available_at=None, event=None):
    cutoff_at = f"{cutoff}T23:59:59.999Z"
    horizon = add_calendar_months(cutoff_at, 12)
    identity = f"github:repository:{group}"
    return {
        "package_id": f"pypi:{name}",
        "cutoff_at": cutoff_at,
        "repo_id": identity,
        "repo_group_id": identity,
        "outcome_end_at": horizon,
        "horizon_end_at": horizon,
        "label_available_at": available_at or horizon,
        "label": label,
        "in_risk_set": True,
        "event_effective_at": horizon if label == 1 else None,
        "event_id": event,
    }


def test_purges_overlapping_horizons_late_labels_and_heldout_groups():
    train_overlap = row("overlap", "2022-06-30", 1)
    train_late = row("late", "2021-12-31", 2, available_at="2023-01-02T00:00:00.000Z")
    train_shared = row("shared-early", "2021-09-30", 3)
    val_shared_event = row("event-early", "2023-06-30", 4, label=1, event="same-notice")
    test_event = row("event-late", "2024-12-31", 5, label=1, event="same-notice")
    holdout_unknown = row("shared-late", "2024-12-31", 3)
    rows = [train_overlap, train_late, train_shared, val_shared_event, test_event]
    result = audit_splits([*rows, holdout_unknown], rows, **TIMES)
    assert result["splits"]["train"]["purged"]["overlapping_horizon"] == 1
    assert result["splits"]["train"]["purged"]["label_unavailable_at_decision"] == 1
    assert result["splits"]["train"]["group_or_event_purged"] == 1
    assert result["splits"]["validation"]["group_or_event_purged"] == 1
    assert result["splits"]["test"]["repo_group_disjoint"]["labels"] == {"0": 0, "1": 1}
    assert result["status"] == "insufficient_classes_after_purge"


def test_retrospective_mode_preserves_real_review_dates_without_claiming_prospective_accuracy():
    import pytest

    now = "2026-01-01T00:00:00.000Z"
    sample = row("reviewed-now", "2021-12-31", 9, available_at=now)
    times = {**TIMES, "fold_fit_at": now, "selection_freeze_at": now}
    with pytest.raises(ValueError, match="out of order"):
        audit_splits([sample], [sample], **times)
    result = audit_splits([sample], [sample], **times, purpose="retrospective_analysis")
    assert result["splits"]["train"]["chronological"]["rows"] == 1
    assert result["prospective_accuracy_claim_supported"] is False
    times["fold_fit_at"] = "2025-12-31T00:00:00.000Z"
    late = audit_splits([sample], [sample], **times, purpose="retrospective_analysis")
    assert late["splits"]["train"]["purged"]["label_unavailable_at_decision"] == 1
