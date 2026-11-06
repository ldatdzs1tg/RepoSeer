"""Find publisher cessation statements in immutable release metadata; never assign labels."""

import argparse
import json
from pathlib import Path

from scripts.collect_d09_batch import archive_timestamp, query_rows, sql_list
from scripts.collect_d09_pilot import run_lock
from scripts.run_d09_drive import canonical_json, digest, jsonl
from tools.coordination._common import atomic_bytes

from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.contracts.identity import package_id
from reposeer.exceptions import HTTPClientError

PHRASES = [
    "no longer maintained",
    "no longer supported",
    "not maintained",
    "project has been discontinued",
    "project is discontinued",
    "end of life",
    "development has stopped",
    "unmaintained",
    "no longer actively maintained",
    "not actively maintained",
    "no longer developed",
    "no further releases",
    "this project is deprecated",
    "this package is deprecated",
    "archived and",
]


def discovery_query(names, *, headlines=False):
    phrases = sql_list(PHRASES)
    searched = "substringUTF8(description, 1, 2500)" if headlines else "description"
    aggregate = "argMax" if headlines else "argMin"
    return f"""SELECT name,
       {aggregate}(tuple(version, toString(upload_time, 'UTC'), sha256_digest, project_urls,
         substringUTF8({searched}, greatest(1, multiSearchFirstPositionCaseInsensitiveUTF8({searched}, [{phrases}]) - 200), 1800)), upload_time) AS notice
FROM pypi.projects
WHERE name IN ({sql_list(names)})
  AND upload_time >= toDateTime64('2019-01-01 00:00:00', 3, 'UTC')
  AND upload_time < toDateTime64('2026-01-01 00:00:00', 3, 'UTC')
  AND multiSearchAnyCaseInsensitiveUTF8({searched}, [{phrases}])
GROUP BY name ORDER BY name
FORMAT JSONEachRow"""


def run(args):
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    body = (args.run_dir / "historical-registry.json").read_bytes()
    registry = json.loads(body)
    packages = registry["packages"]
    if digest(canonical_json(packages)) != registry["packages_sha256"]:
        raise ValueError("frozen registry checksum differs")
    subjects = sorted(
        (
            row
            for row in packages.values()
            if args.include_newer or row["first_upload_at"] < "2020-01-01T00:00:00.000Z"
        ),
        key=lambda row: (row["first_upload_at"], row["package_id"]),
    )
    names = list(dict.fromkeys(name for row in subjects for name in row["source_names"]))
    leads, processed = [], 0
    with HTTPClient(timeout=60, max_retries=2) as http:
        for index, offset in enumerate(range(0, len(names), 1000)):
            if args.max_shards and index >= args.max_shards:
                break
            group = names[offset : offset + 1000]
            rows = query_rows(
                http,
                output,
                f"publisher-notices-{index:03d}",
                discovery_query(group, headlines=args.headlines),
                post=True,
            )
            for row in rows:
                if row["name"] not in group:
                    raise ValueError("out-of-cohort publisher notice")
                version, timestamp, sha, links, excerpt = row["notice"]
                leads.append(
                    {
                        "package_id": package_id(row["name"]),
                        "source_name": row["name"],
                        "source_version": version,
                        "uploaded_at": archive_timestamp(timestamp),
                        "artifact_sha256": sha,
                        "project_urls": links,
                        "excerpt": excerpt,
                        "review_status": "unreviewed_lead",
                        "label": None,
                    }
                )
            processed += len(group)
            print(
                f"Publisher discovery: {processed}/{len(names)} names, {len(leads)} unreviewed leads",
                flush=True,
            )
    atomic_bytes(output / "publisher-notices.jsonl", jsonl(leads), immutable=False)
    result = {
        "names_examined": processed,
        "names_total": len(names),
        "unreviewed_leads": len(leads),
        "labels_assigned": 0,
        "registry_sha256": digest(body),
        "selection": "Drive historical cohort, oldest packages first; discovery only",
        "includes_newer_packages": args.include_newer,
        "headlines_only": args.headlines,
    }
    write_json_atomic(output / "summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=Path("data/d09/drive-full-v1"))
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data/d09/publisher-outcome-discovery-v1")
    )
    parser.add_argument(
        "--max-shards", type=int, default=3, help="Bounded batches of 1000 names; 0 for all"
    )
    parser.add_argument(
        "--include-newer",
        action="store_true",
        help="Include every package in the frozen historical registry",
    )
    parser.add_argument(
        "--headlines",
        action="store_true",
        help="Find the latest matching notice in the first 2500 characters; discovery only",
    )
    args = parser.parse_args()
    if args.max_shards < 0:
        parser.error("max-shards must be nonnegative")
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with run_lock(args.output_dir):
            result = run(args)
    except (OSError, ValueError, HTTPClientError) as error:
        parser.exit(1, f"Publisher discovery stopped; rerun to resume checked queries: {error}\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
