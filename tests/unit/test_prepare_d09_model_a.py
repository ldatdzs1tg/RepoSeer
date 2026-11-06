from copy import deepcopy

from scripts.build_d09_reviewed import build_reviewed
from scripts.prepare_d09_model_a import build_target, checked_release_census

CUTOFF = "2021-12-31T23:59:59.999Z"
NOW = "2026-11-05T12:00:00.000Z"
HISTORY = [
    {
        "version": "1",
        "uploaded_at": "2020-01-01T00:00:00.000Z",
        "sha256": "a" * 64,
        "requires_python": ">=3.8",
    },
    {
        "version": "2",
        "uploaded_at": "2022-01-01T00:00:00.000Z",
        "sha256": "b" * 64,
        "requires_python": ">=3.8",
    },
]
PROJECT = {
    "info": {"name": "demo"},
    "releases": {
        row["version"]: [
            {"upload_time_iso_8601": row["uploaded_at"], "digests": {"sha256": row["sha256"]}}
        ]
        for row in HISTORY
    },
}


def test_release_census_ignores_future_versions_and_rejects_missing_or_changed_artifacts():
    before, complete = checked_release_census("pypi:demo", HISTORY, PROJECT, CUTOFF)
    assert complete and [row["version"] for row in before] == ["1"]
    changed = deepcopy(PROJECT)
    changed["releases"]["1"][0]["digests"]["sha256"] = "c" * 64
    assert checked_release_census("pypi:demo", HISTORY, changed, CUTOFF)[1] is False
    changed["releases"].pop("1")
    assert checked_release_census("pypi:demo", HISTORY, changed, CUTOFF)[1] is False


def test_restored_original_requires_available_timestamp_and_cannot_override_earlier_upload():
    project = deepcopy(PROJECT)
    project["releases"]["1"][0] = {
        "upload_time_iso_8601": "2020-02-01T00:00:00.000Z",
        "digests": {"sha256": "c" * 64},
    }
    restored = {HISTORY[0]["sha256"]}
    rows, complete = checked_release_census("pypi:demo", HISTORY, project, CUTOFF, restored)
    assert complete
    assert rows[0]["available_at"] == "2020-01-01T00:00:00.001Z"
    assert not checked_release_census(
        "pypi:demo", HISTORY, project, HISTORY[0]["uploaded_at"], restored
    )[1]
    project["releases"]["1"][0]["upload_time_iso_8601"] = "2019-12-31T23:59:59.999Z"
    assert not checked_release_census("pypi:demo", HISTORY, project, CUTOFF, restored)[1]


def test_built_release_features_do_not_turn_maintenance_leads_into_negative_labels(tmp_path):
    pypi = {
        "url": "https://pypi.org/pypi/demo/1/json",
        "retrieved_at": NOW,
        "content_sha256": "a" * 64,
        "artifact_uploaded_at": HISTORY[0]["uploaded_at"],
    }
    archive = {
        "url": "https://data.gharchive.org/2021-06-01-12.json.gz",
        "retrieved_at": NOW,
        "content_sha256": "b" * 64,
        "revision": {"timeCreated": "2021-06-01T13:05:00.000Z", "generation": "1"},
    }
    candidate = {
        "package_id": "pypi:demo",
        "cutoff_at": CUTOFF,
        "repo_id": "github:repository:123",
        "repo_url": "https://github.com/org/demo",
        "distribution_sha256": "a" * 64,
        "pypi_source": pypi,
        "archive_source": archive,
        "archive_event": {"event_at": "2021-06-01T12:00:00.000Z", "record_sha256": "c" * 64},
    }
    mirror = {
        "url": "https://example.com/mirror",
        "retrieved_at": NOW,
        "content_sha256": "d" * 64,
        "query_sha256": "e" * 64,
    }
    project_source = {
        "url": "https://pypi.org/pypi/demo/json",
        "retrieved_at": NOW,
        "content_sha256": "f" * 64,
    }
    review, checks = build_target(
        candidate,
        {"first_upload_at": HISTORY[0]["uploaded_at"]},
        HISTORY,
        mirror,
        PROJECT,
        project_source,
        NOW,
        census_dir=tmp_path,
    )
    assert checks == {"cutoff_release_census": True, "horizon_release_census": True}
    snapshots, labels, strict, _ = build_reviewed(
        {("pypi:demo", CUTOFF): HISTORY[0]["uploaded_at"]}, [review.model_dump()], NOW
    )
    assert snapshots["releases_total"].to_list() == [1]
    assert snapshots["release_count_12m"].to_list() == [0]
    assert snapshots["commit_count_12m"].to_list() == [None]
    assert labels["label_status"].to_list() == ["unknown"]
    assert strict.is_empty()
    assert list(tmp_path.glob("*.json"))
