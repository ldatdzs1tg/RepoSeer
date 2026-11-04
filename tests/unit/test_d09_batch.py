from datetime import datetime

import pytest
from scripts.audit_d09_archive import window_audit
from scripts.collect_d09_batch import review_rows, roster_rows


def test_review_rows_keep_postcutoff_version_out_of_mapping():
    roster = roster_rows(
        [{"project": "demo-pkg", "names": ["Demo_Pkg"], "first_upload_at": "2024-09-12 15:00:00"}]
    )
    releases = [
        {
            "version": "1",
            "uploaded_at": "2024-09-12T15:00:00.000Z",
            "sha256": "a" * 64,
            "project_urls": ["Source, https://github.com/org/old"],
        },
        {
            "version": "2",
            "uploaded_at": "2024-10-12T15:00:00.000Z",
            "sha256": "b" * 64,
            "project_urls": ["Source, https://github.com/org/new"],
        },
    ]
    first, second = review_rows(roster, {"pypi:demo-pkg": releases})
    assert first["source_version"] == "1"
    assert first["archive_names"] == ["org/old"]
    assert first["observed_horizon_releases"] == 1
    assert second["archive_names"] == ["org/new"]
    with pytest.raises(ValueError, match="frozen roster"):
        list(review_rows(roster, {"pypi:demo-pkg": releases[1:]}))


def test_archive_inventory_distinguishes_missing_from_late_revision():
    start = "2024-09-30T21:59:59.999Z"
    end = "2024-09-30T23:59:59.999Z"
    last_hour = "2024-09-30T23:00:00.000Z"
    result = window_audit(
        {datetime.fromisoformat(last_hour): {"timeCreated": "2024-10-01T00:05:00.000Z"}},
        start,
        end,
    )
    assert result["expected_hours"] == 2
    assert result["missing_hours"] == ["2024-09-30T22:00:00.000Z"]
    assert result["object_revisions_created_after_window_end"] == [last_hour]
