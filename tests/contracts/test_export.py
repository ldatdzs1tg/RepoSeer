"""Artifacts come from models, are deterministic and detect drift without writes."""

import json
import subprocess
import sys

import pytest
from scripts.export_contract import ROOT, check_artifacts, generate_artifacts, ts_type
from scripts.validate_examples import validate_examples


def test_generated_artifacts_match_and_nullable_required_properties():
    artifacts = generate_artifacts()
    assert artifacts == generate_artifacts()
    assert check_artifacts(ROOT / "docs/contracts", artifacts) == []
    schema = json.loads(artifacts["data_contract.schema.json"])
    assert schema["$schema"].endswith("2020-12/schema")
    for name, model in schema["$defs"].items():
        if "properties" in model:
            assert set(model["required"]) == set(model["properties"]), name
    ts = artifacts["types.ts"].decode("utf-8")
    assert "http_status: number | null;" in ts
    assert "http_status?:" not in ts
    assert "Promise<CollectionResult>" in ts
    assert "any" not in ts.split()


def test_model_change_produces_drift_and_check_never_writes(tmp_path, monkeypatch):
    import scripts.export_contract as exporter

    expected = generate_artifacts()
    for name, data in expected.items():
        (tmp_path / name).write_bytes(data)
    from functools import reduce
    from operator import or_
    from typing import Annotated, get_args

    from pydantic import Field, TypeAdapter

    from reposeer.contracts import ContractRecord, RawRecord

    class ChangedRawRecord(RawRecord):
        drift_probe: str | None

    kinds = get_args(get_args(ContractRecord)[0])
    changed_union = reduce(or_, (ChangedRawRecord if kind is RawRecord else kind for kind in kinds))
    changed_adapter = TypeAdapter(Annotated[changed_union, Field(discriminator="record_type")])
    monkeypatch.setattr(exporter, "CONTRACT_ADAPTER", changed_adapter)
    assert "data_contract.schema.json" in check_artifacts(tmp_path, generate_artifacts())
    assert {name: (tmp_path / name).read_bytes() for name in expected} == expected
    # Exercise actual CLI nonzero/no-write behavior against an isolated artifact root.
    (tmp_path / "docs/contracts").mkdir(parents=True)
    monkeypatch.setattr(exporter, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["export_contract.py", "--check"])
    assert exporter.main() == 1
    assert list((tmp_path / "docs/contracts").iterdir()) == []


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "patternProperties": {}},
        {"allOf": [{"type": "string"}]},
        {"type": "mystery"},
        {"type": "array"},
        {"$ref": "https://example.test/schema"},
    ],
)
def test_unsupported_shapes_fail_clearly(schema):
    with pytest.raises(ValueError):
        ts_type(schema)


def test_ts_arrays_maps_unions_and_unconstrained_json():
    assert ts_type({"type": "array", "items": {"type": "integer"}}) == "Array<number>"
    assert ts_type({"type": ["string", "null"]}) == "string | null"
    assert (
        ts_type({"type": "object", "additionalProperties": {"type": "boolean"}})
        == "{ [key: string]: boolean; }"
    )
    assert ts_type({}) == "unknown"


def test_all_synthetic_examples_and_pair_checks():
    assert validate_examples(ROOT / "examples/contracts") == 19


@pytest.mark.parametrize("change", ["duplicate", "pair_mismatch", "invalid"])
def test_example_validator_rejects_duplicate_mismatch_or_invalid(tmp_path, change):
    for source in (ROOT / "examples/contracts").glob("*.json"):
        (tmp_path / source.name).write_bytes(source.read_bytes())
    if change == "duplicate":
        (tmp_path / "duplicate.json").write_bytes(
            (tmp_path / "github_raw_record.json").read_bytes()
        )
    else:
        path = tmp_path / "collection_result.json"
        data = json.loads(path.read_bytes())
        data["request_id" if change == "pair_mismatch" else "schema_version"] = (
            "00000000-0000-4000-8000-000000000099" if change == "pair_mismatch" else "0.3"
        )
        path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        validate_examples(tmp_path)


def test_export_check_cli():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/export_contract.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
