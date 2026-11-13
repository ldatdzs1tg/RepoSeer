import json
from types import SimpleNamespace

import pytest
from scripts import audit_d09_readiness as readiness
from scripts.audit_model_a_splits import audit_splits
from scripts.run_d09_drive import SPLIT_TIMES, digest, shard_reviews_digest

from reposeer.collection.persistence import write_json_atomic
from reposeer.pipelines.maintenance import MaintenancePipeline


def checkpoint(root, name, status):
    directory = root / "batches" / name
    output = directory / "verification"
    output.mkdir(parents=True)
    raw = json.dumps({"status": status}).encode() + b"\n"
    (output / "mapping_evidence.jsonl").write_bytes(raw)
    revision = shard_reviews_digest(directory)
    write_json_atomic(directory / "collection.json", {"source_reviews_sha256": revision})
    write_json_atomic(
        output / "summary.json",
        {
            "path": "mapping_evidence.jsonl",
            "content_sha256": digest(raw),
            "source_reviews_sha256": revision,
            "statuses": {status: 1},
        },
    )
    return directory


def test_freezes_completed_batches_and_rejects_tampered_completed_evidence(tmp_path):
    complete = checkpoint(tmp_path, "batch-00000", "mapping_sources_matched")
    checkpoint(tmp_path, "batch-00001", "archive_download_pending")
    batches, pending = readiness.completed_batches(tmp_path)
    assert [path.name for path, _ in batches] == ["batch-00000"]
    assert pending == ["batch-00001"]
    (complete / "verification/mapping_evidence.jsonl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity checks"):
        readiness.completed_batches(tmp_path)


def test_new_verification_date_makes_a_mature_negative_unavailable_for_the_old_fit():
    cutoff = "2021-12-31T23:59:59.999Z"
    verified_at = "2026-11-05T12:00:00.000Z"
    label = MaintenancePipeline().evaluate_horizon_label(
        package_id="pypi:demo",
        repo_id="github:repository:123",
        cutoff_at=cutoff,
        observation_end_at=verified_at,
        horizon_coverage_complete=True,
        horizon_coverage_available_at=verified_at,
        horizon_releases=[
            SimpleNamespace(
                package_id="pypi:demo",
                repo_id="github:repository:123",
                uploaded_at="2022-03-01T00:00:00.000Z",
                provenance=SimpleNamespace(
                    valid_at="2022-03-01T00:00:00.000Z",
                    available_at="2022-03-01T00:00:00.000Z",
                ),
            )
        ],
    )
    assert label.label == 0 and label.label_available_at == verified_at
    row = {**label.model_dump(), "outcome_end_at": label.horizon_end_at}
    result = audit_splits([row], [row], **SPLIT_TIMES, evaluation_as_of=verified_at)
    assert result["splits"]["train"]["purged"]["label_unavailable_at_decision"] == 1
    timing = readiness.new_label_timing(verified_at)
    assert timing["splits_accepting_a_label_first_verified_now"] == {
        "train": False,
        "validation": False,
        "test": True,
    }


def test_unrelated_pilot_reviews_are_counted_without_promoting_them_into_the_cohort(tmp_path):
    path = tmp_path / "reviews.jsonl"
    path.write_text(
        json.dumps(
            {"snapshot": {"package_id": "pypi:other", "cutoff_at": "2024-01-01T00:00:00.000Z"}}
        )
        + "\n"
    )
    sources = {}
    reviews, catalog = readiness.read_reviews(
        [path], {("pypi:demo", "2024-01-01T00:00:00.000Z")}, "2026-11-05T00:00:00.000Z", sources
    )
    assert reviews == {}
    assert catalog[0]["rows"] == 1 and catalog[0]["matching_rows"] == 0
    assert sources[str(path.resolve())] == digest(path.read_bytes())


@pytest.mark.parametrize("assigned_label", [0, 1])
def test_source_observations_cannot_be_used_as_supervised_labels(tmp_path, assigned_label):
    path = tmp_path / "source-checks.jsonl"
    path.write_text(json.dumps({"label_assigned": assigned_label}) + "\n")
    with pytest.raises(ValueError, match="cannot assign labels"):
        readiness.source_checks(path, "2026-11-05T00:00:00.000Z", {})
