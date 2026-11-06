"""Load a frozen Model A and evaluate a verified cutoff snapshot."""

import argparse
import json
from pathlib import Path

from reposeer.contracts.models import PackageSnapshot
from reposeer.modeling.model_a import ModelA


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--experimental", action="store_true", help="Include a development score")
    args = parser.parse_args()
    try:
        snapshot = PackageSnapshot.model_validate_json(args.snapshot.read_bytes())
        result = ModelA.load(args.model).predict_snapshot(snapshot, experimental=args.experimental)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f"Model A inference stopped: {error}\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
