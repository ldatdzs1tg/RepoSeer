"""Validate synthetic contract fixtures, references and all four I/O pairs."""

from pathlib import Path

from reposeer.contracts import (
    RawRecord,
    validate_collection_pair,
    validate_normalization_pair,
    validate_record,
    validate_snapshot_pair,
    validate_technology_link_pair,
)

ROOT = Path(__file__).resolve().parents[1]


def validate_examples(directory: Path) -> int:
    records = {}
    identities = set()
    for path in sorted(directory.glob("*.json")):
        record = validate_record(path.read_bytes())
        records[path.stem] = record
        kind = record.record_type
        if kind == "github_repository":
            identity = (kind, record.identity.repo_id)
        elif kind == "package_release":
            identity = (kind, record.package_id, record.version)
        elif kind == "package_snapshot":
            identity = (kind, record.package_id, record.cutoff_at)
        else:
            field = {
                "raw_record": "raw_record_id",
                "technology_entity": "technology_id",
                "article": "article_id",
                "repository_activity": "activity_id",
            }.get(kind, "request_id")
            identity = (kind, getattr(record, field))
        if identity in identities:
            raise ValueError(f"duplicate fixture identity in {path.name}")
        identities.add(identity)
    required = {
        "github_raw_record",
        "github_repository",
        "technology_entity",
        "article",
        "package_release",
        "repository_activity",
        "package_snapshot",
        "collection_request",
        "collection_result",
        "normalization_request",
        "normalization_result",
        "technology_link_request",
        "technology_link_result",
        "snapshot_build_request",
        "snapshot_build_result",
    }
    if not required <= records.keys():
        raise ValueError(
            "missing required fixture files: " + ", ".join(sorted(required - records.keys()))
        )
    for prefix, checker in (
        ("collection", validate_collection_pair),
        ("normalization", validate_normalization_pair),
        ("technology_link", validate_technology_link_pair),
        ("snapshot_build", validate_snapshot_pair),
    ):
        checker(records[prefix + "_request"], records[prefix + "_result"])
    raw = {
        record.raw_record_id: record for record in records.values() if isinstance(record, RawRecord)
    }

    def check_references(record):
        provenance = getattr(record, "provenance", None)
        if provenance:
            for raw_id in provenance.raw_record_ids:
                if raw_id not in raw or raw[raw_id].source != provenance.source:
                    raise ValueError("orphan/source-mismatched example provenance")
        for field in ("records", "articles", "technology_entities", "releases", "activities"):
            for nested in getattr(record, field, []):
                if isinstance(nested, RawRecord):
                    if nested != raw.get(nested.raw_record_id):
                        raise ValueError("nested raw fixture mismatch")
                else:
                    check_references(nested)

    for record in records.values():
        check_references(record)
    if records["collection_result"].records != records["normalization_request"].records:
        raise ValueError("collection-to-normalization fixture mismatch")
    if records["normalization_result"].records != [records["github_repository"]]:
        raise ValueError("normalized standalone fixture mismatch")
    if records["technology_link_request"].technology_entities != [records["technology_entity"]]:
        raise ValueError("taxonomy standalone fixture mismatch")
    if records["snapshot_build_request"].repository != records["github_repository"].identity:
        raise ValueError("snapshot repository identity fixture mismatch")
    if records["technology_link_request"].articles != [records["article"]]:
        raise ValueError("article standalone fixture mismatch")
    snapshot_request = records["snapshot_build_request"]
    if snapshot_request.releases != [records["package_release"]] or snapshot_request.activities != [
        records["repository_activity"]
    ]:
        raise ValueError("snapshot evidence fixture mismatch")
    if records["snapshot_build_result"].snapshot != records["package_snapshot"]:
        raise ValueError("snapshot standalone fixture mismatch")
    return len(records)


def main() -> int:
    try:
        count = validate_examples(ROOT / "examples/contracts")
    except (ValueError, TypeError) as error:
        print(f"Invalid contract examples: {error}")
        return 1
    print(f"Validated {count} synthetic fixtures and all four request/result pairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
