"""Capture bounded first-party histories for independent D09 outcome adjudication."""

import argparse
import json
from pathlib import Path

from scripts.collect_d09_batch import parse_rows
from scripts.collect_d09_pilot import fetch_public, run_lock, stored_source_matches
from scripts.run_d09_drive import digest
from tools.coordination._common import atomic_bytes

from reposeer.collection.http.client import HTTPClient
from reposeer.collection.persistence import write_json_atomic
from reposeer.exceptions import HTTPClientError


def capture(http, directory, key, url, *, text=False):
    receipt_path = directory / f"{key}.receipt.json"
    source = json.loads(receipt_path.read_bytes()) if receipt_path.exists() else {}
    if source.get("url") == url and stored_source_matches(directory, source):
        body = (directory / source["path"]).read_bytes()
    else:
        body, source = fetch_public(http, url, 8 * 1024 * 1024)
        source["path"] = f"{key}-{digest(body)}.{'txt' if text else 'json'}"
        atomic_bytes(directory / source["path"], body, immutable=False)
        write_json_atomic(receipt_path, source)
    return (body.decode("utf-8") if text else json.loads(body)), source


def run(args):
    root = args.output_dir
    candidates = {}
    for path in args.cohort.glob("batches/*/shard-*/review.jsonl"):
        for row in parse_rows(path.read_bytes()):
            if len(row["observed_repo_ids"]) == 1:
                candidates[row["package_id"]] = row["observed_repo_ids"][0]
    reports = []
    if args.package:
        wanted = {"pypi:" + name.removeprefix("pypi:") for name in args.package}
        candidates = {
            subject: identity for subject, identity in candidates.items() if subject in wanted
        }
    with HTTPClient(timeout=30, max_retries=1) as http:
        for subject, identity in sorted(candidates.items()):
            folder = root / subject.removeprefix("pypi:")
            folder.mkdir(parents=True, exist_ok=True)
            repo, _ = capture(
                http,
                folder,
                "repository",
                f"https://api.github.com/repositories/{identity.split(':')[-1]}",
            )
            if str(repo["id"]) != identity.split(":")[-1]:
                raise ValueError("repository identity changed")
            report = {
                "package_id": subject,
                "repo_id": identity,
                "repo_full_name": repo["full_name"],
            }
            for kind, parameters in (
                ("releases", ""),
                ("issues", "&state=all&sort=created&direction=asc"),
                ("issues/comments", "&sort=created&direction=asc"),
            ):
                key = "comments" if kind == "issues/comments" else kind
                count, complete = 0, False
                for page in range(1, args.max_pages + 1):
                    url = f"https://api.github.com/repositories/{repo['id']}/{kind}?per_page=100&page={page}{parameters}"
                    payload, _ = capture(http, folder, f"{key}-{page:03d}", url)
                    if not isinstance(payload, list):
                        raise ValueError("expected paginated first-party history")
                    count += len(payload)
                    if len(payload) < 100:
                        complete = True
                        break
                report[key + "_complete"] = complete
                report[key + "_records"] = count
            readme = False
            for filename in ("README.md", "README.rst", "README"):
                try:
                    capture(
                        http,
                        folder,
                        "readme",
                        f"https://raw.githubusercontent.com/{repo['full_name']}/{repo['default_branch']}/{filename}",
                        text=True,
                    )
                except HTTPClientError as error:
                    if error.status_code == 404:
                        continue
                    raise
                readme = True
                break
            report["readme_captured"] = readme
            report["outcome_adjudicated"] = False
            write_json_atomic(folder / "collection.json", report)
            reports.append(report)
            print(json.dumps(report), flush=True)
    write_json_atomic(root / "summary.json", {"subjects": reports, "labels_assigned": 0})
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cohort", type=Path, default=Path("data/d09/model-a-development-cohort-v1")
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("data/d09/model-a-outcome-reviews-v1")
    )
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--package", action="append", default=[])
    args = parser.parse_args()
    if not 1 <= args.max_pages <= 100:
        parser.error("expected 1–100 pages per history")
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with run_lock(args.output_dir):
            run(args)
    except (OSError, ValueError, HTTPClientError) as error:
        parser.exit(
            1, f"Outcome source capture stopped; rerun to resume checked responses: {error}\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
