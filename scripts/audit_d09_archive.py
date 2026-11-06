"""Inventory GH Archive object revisions and report gaps at D09 cutoffs."""

import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from scripts.collect_d09_batch import CUTOFFS, WINDOW_END, WINDOW_START
from scripts.collect_d09_pilot import fetch_public, run_lock, stored_source_matches
from tools.coordination._common import atomic_bytes

from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.identity import add_calendar_months, format_utc_timestamp
from reposeer.exceptions import HTTPClientError

BUCKET_URL = "https://storage.googleapis.com/storage/v1/b/data.gharchive.org/o"
HOUR = timedelta(hours=1)
OBJECT_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})-(0|[1-9]|1[0-9]|2[0-3])\.json\.gz")


def inventory(http: HTTPClient, directory: Path, *, start_year=None, end_year=None) -> dict:
    objects = {}
    for year in range(start_year or int(WINDOW_START[:4]), (end_year or int(WINDOW_END[:4])) + 1):
        print(f"Inventorying GH Archive {year}", flush=True)
        params = {
            "prefix": f"{year}-",
            "maxResults": "1000",
            "fields": "nextPageToken,items(name,generation,timeCreated,md5Hash,size)",
        }
        tokens = set()
        for index in range(20):
            url = BUCKET_URL + "?" + urlencode(params)
            path = directory / "sources" / f"{year}-{index:02d}.json"
            receipt = json.loads(path.read_bytes()) if path.exists() else {}
            if receipt.get("url") == url and stored_source_matches(directory, receipt):
                body = (directory / receipt["path"]).read_bytes()
            else:
                body, receipt = fetch_public(http, url, 2 * 1024 * 1024)
                raw_path = path.with_suffix(f".{receipt['content_sha256']}.raw.json")
                atomic_bytes(raw_path, body, immutable=False)
                write_json_atomic(
                    path, {**receipt, "path": raw_path.relative_to(directory).as_posix()}
                )
            page = json.loads(body)
            for item in page.get("items", []):
                match = OBJECT_NAME.fullmatch(item["name"])
                if match is None:
                    continue
                hour = datetime.fromisoformat(f"{match[1]}T{int(match[2]):02d}:00:00+00:00")
                created_at = datetime.fromisoformat(item["timeCreated"])
                if (
                    hour.year != year
                    or hour in objects
                    or created_at.tzinfo is None
                    or created_at < hour
                    or not item["generation"].isdecimal()
                    or int(item["size"]) <= 0
                ):
                    raise ValueError("invalid GH Archive object revision")
                objects[hour] = {
                    **item,
                    "inventory_receipt": path.relative_to(directory).as_posix(),
                }
            token = page.get("nextPageToken")
            if token is None:
                break
            if not isinstance(token, str) or token in tokens:
                raise ValueError("invalid GH Archive pagination token")
            tokens.add(token)
            params["pageToken"] = token
        else:
            raise ValueError(f"GH Archive inventory pagination limit reached in {year}")
    return objects


def window_audit(objects: dict, start_at: str, end_at: str) -> dict:
    start, end = datetime.fromisoformat(start_at), datetime.fromisoformat(end_at)
    hour = start.replace(minute=0, second=0, microsecond=0)
    # D09 windows are open on the left. This hour has no representable event
    # after the left bound when it ends one millisecond before the next hour.
    if start == hour + HOUR - timedelta(milliseconds=1):
        hour += HOUR
    missing, late = [], []
    expected = 0
    while hour <= end:
        expected += 1
        revision = objects.get(hour)
        if revision is None:
            missing.append(format_utc_timestamp(hour))
        elif datetime.fromisoformat(revision["timeCreated"]) > end:
            late.append(format_utc_timestamp(hour))
        hour += HOUR
    return {
        "start_at": start_at,
        "end_at": end_at,
        "expected_hours": expected,
        "missing_hours": missing,
        "object_revisions_created_after_window_end": late,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--start-year", type=int)
    parser.add_argument("--end-year", type=int)
    args = parser.parse_args()
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with run_lock(args.output_dir), HTTPClient(timeout=45, max_retries=2) as http:
            objects = inventory(http, args.output_dir, start_year=args.start_year, end_year=args.end_year)
            write_json_atomic(
                args.output_dir / "inventory.json",
                {
                    format_utc_timestamp(hour): revision
                    for hour, revision in sorted(objects.items())
                },
            )
            write_json_atomic(
                args.output_dir / "summary.json",
                {
                    "retrieved_at": format_utc_timestamp(datetime.now(UTC)),
                    "object_revisions_observed": len(objects),
                    "scope": "GH Archive object inventory",
                    "event_census_verified": False,
                    "cutoffs": [
                        {
                            "cutoff_at": cutoff,
                            "lookbacks": {
                                str(months): window_audit(
                                    objects, add_calendar_months(cutoff, -months), cutoff
                                )
                                for months in (3, 6, 12)
                            },
                            "outcome_horizon": window_audit(
                                objects, cutoff, add_calendar_months(cutoff, 12)
                            ),
                        }
                        for cutoff in CUTOFFS
                    ],
                },
            )
    except (HTTPClientError, OSError, ValueError) as error:
        parser.exit(1, f"GH Archive inventory stopped: {error}\n")
    print(f"GH Archive inventory: {args.output_dir.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
