"""Validate the additive synthetic FinalReport examples without running inference."""

from pathlib import Path

from reposeer.contracts.analysis import validate_final_report

ROOT = Path(__file__).resolve().parents[1]


def validate_examples(directory: Path) -> int:
    paths = sorted(directory.glob("final_report*.json"))
    if not paths or not (directory / "final_report.json").is_file():
        raise ValueError("final_report.json and analysis examples are required")
    statuses = set()
    for path in paths:
        report = validate_final_report(path.read_bytes())
        if validate_final_report(report.model_dump_json()) != report:
            raise ValueError(f"report round-trip mismatch: {path.name}")
        statuses.add(report.result_status)
    if not {"predicted", "already_abandoned", "scheduled_cessation", "mapping_unresolved"} <= statuses:
        raise ValueError("examples must demonstrate prediction, both known statuses and no-score")
    return len(paths)


def main() -> int:
    try:
        count = validate_examples(ROOT / "examples/analysis")
    except (ValueError, TypeError) as error:
        print(f"Invalid analysis examples: {error}")
        return 1
    print(f"Validated {count} synthetic FinalReport examples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
