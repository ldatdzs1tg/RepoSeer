"""Check chronological Model A split feasibility from a reviewed D09 run."""

import argparse
import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import polars as pl

from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.identity import (
    add_calendar_months,
    checked_timestamp,
    normalize_pypi_name,
    parse_repo_id,
)

SPLITS = ("train", "validation", "test")


def audit_splits(
    universe: Iterable[dict],
    strict_rows: Iterable[dict],
    *,
    fold_fit_at: str,
    validation_start_at: str,
    selection_freeze_at: str,
    test_start_at: str,
    test_end_at: str,
    evaluation_as_of: str,
) -> dict:
    times = {
        "fold_fit_at": fold_fit_at,
        "validation_start_at": validation_start_at,
        "selection_freeze_at": selection_freeze_at,
        "test_start_at": test_start_at,
        "test_end_at": test_end_at,
        "evaluation_as_of": evaluation_as_of,
    }
    for timestamp in times.values():
        checked_timestamp(timestamp)
    if not (
        fold_fit_at
        < validation_start_at
        < selection_freeze_at
        < test_start_at
        < test_end_at
        <= evaluation_as_of
    ):
        raise ValueError("split windows and decision times are out of order")

    def split_at(cutoff: str) -> str | None:
        if cutoff < validation_start_at:
            return "train"
        if cutoff < test_start_at:
            return "validation"
        return "test" if cutoff < test_end_at else None

    seen = {}
    cohorts = Counter()
    groups = {split: set() for split in SPLITS}
    for row in universe:
        package, cutoff = row["package_id"], checked_timestamp(row["cutoff_at"])
        name = package.removeprefix("pypi:")
        if package != f"pypi:{normalize_pypi_name(name)}" or (package, cutoff) in seen:
            raise ValueError("invalid or duplicate D09 eligibility row")
        group = row.get("repo_group_id")
        if group is not None:
            parse_repo_id(group)
        seen[(package, cutoff)] = group
        split = split_at(cutoff)
        if split is not None:
            cohorts[split] += 1
            if group is not None:
                groups[split].add(group)

    eligible = {split: Counter() for split in SPLITS}
    counts = {split: Counter() for split in SPLITS}
    decisions = {"train": fold_fit_at, "validation": selection_freeze_at, "test": evaluation_as_of}
    boundaries = {"train": validation_start_at, "validation": test_start_at}
    strict_keys = set()
    for row in strict_rows:
        key = (row["package_id"], row["cutoff_at"])
        cutoff, horizon = checked_timestamp(key[1]), checked_timestamp(row["outcome_end_at"])
        label_at = checked_timestamp(row["label_available_at"])
        label, group = row["label"], row["repo_group_id"]
        event_id = row.get("event_id")
        if (
            key in strict_keys
            or seen.get(key) is None
            or seen[key] != group
            or row["repo_id"] != group
            or horizon != add_calendar_months(cutoff, 12)
            or row["horizon_end_at"] != horizon
            or row["in_risk_set"] is not True
            or type(label) is not int
            or label not in (0, 1)
            or label_at <= cutoff
            or (label == 0 and label_at < horizon)
            or (event_id is not None and (not isinstance(event_id, str) or not event_id.strip()))
        ):
            raise ValueError(f"invalid strict D09 row: {key}")
        if label == 1:
            event_at = checked_timestamp(row["event_effective_at"])
            if not cutoff < event_at <= min(horizon, label_at):
                raise ValueError(f"positive cessation lies outside its eligible horizon: {key}")
        strict_keys.add(key)
        split = split_at(cutoff)
        if split is None:
            continue
        counts[split]["strict_rows_before_purge"] += 1
        if split in boundaries and horizon >= boundaries[split]:
            counts[split]["overlapping_horizon"] += 1
        elif horizon > decisions[split]:
            counts[split]["horizon_not_mature_at_decision"] += 1
        elif label_at > decisions[split]:
            counts[split]["label_unavailable_at_decision"] += 1
        else:
            eligible[split][(group, event_id, label)] += 1

    def stats(rows: Counter) -> dict:
        classes = Counter()
        for (_, _, label), count in rows.items():
            classes[label] += count
        return {
            "rows": classes.total(),
            "labels": {str(label): classes[label] for label in (0, 1)},
            "repo_groups": len({group for group, _, _ in rows}),
            "both_classes": bool(classes[0] and classes[1]),
        }

    later_groups, later_events = set(), set()
    audits = {}
    for split in reversed(SPLITS):
        strict = Counter(
            {
                (group, event_id, label): count
                for (group, event_id, label), count in eligible[split].items()
                if group not in later_groups and event_id not in later_events
            }
        )
        audits[split] = {
            "cohort_rows": cohorts[split],
            "strict_rows_before_purge": counts[split]["strict_rows_before_purge"],
            "purged": {
                key: counts[split][key]
                for key in (
                    "overlapping_horizon",
                    "horizon_not_mature_at_decision",
                    "label_unavailable_at_decision",
                )
            },
            "chronological": stats(eligible[split]),
            "repo_group_disjoint": stats(strict),
            "group_or_event_purged": eligible[split].total() - strict.total(),
        }
        later_groups.update(groups[split])
        later_events.update(event_id for _, event_id, _ in eligible[split] if event_id is not None)
    return {
        "status": "both_classes_in_each_disjoint_split"
        if all(audit["repo_group_disjoint"]["both_classes"] for audit in audits.values())
        else "insufficient_classes_after_purge",
        "decision_times_utc": times,
        "group_priority": list(reversed(SPLITS)),
        "input_strict_rows": len(strict_keys),
        "splits": {split: audits[split] for split in SPLITS},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--d09-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    for name in (
        "fold-fit-at",
        "validation-start-at",
        "selection-freeze-at",
        "test-start-at",
        "test-end-at",
        "evaluation-as-of",
    ):
        parser.add_argument(f"--{name}", required=True)
    args = vars(parser.parse_args())
    directory, output = args.pop("d09_dir"), args.pop("output")
    try:
        summary = json.loads((directory / "summary.json").read_bytes())
        universe = json.loads((directory / "eligibility.json").read_bytes())
        strict = directory / "strict_e0_rows.parquet"
        rows = pl.read_parquet(strict).to_dicts() if strict.is_file() else []
        if len(rows) != summary["strict_rows"] or len(universe) != summary["universe_rows"]:
            raise ValueError("D09 artifacts do not match the build summary")
        audit = audit_splits(universe, rows, **args)
        write_json_atomic(output, audit)
    except (OSError, ValueError, KeyError, pl.exceptions.PolarsError) as error:
        parser.exit(1, f"Model A split audit stopped: {error}\n")
    print(f"Model A split audit: {output.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
