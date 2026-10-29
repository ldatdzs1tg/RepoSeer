"""Analyze an explicit offline T-025 request; no network or implicit data scans."""
import argparse
from pathlib import Path

from reposeer.analysis.external import ExternalContextRequest, analyze_external_context


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Explicit ExternalContextRequest JSON")
    parser.add_argument("--output", type=Path, help="Optional NEW output file (never overwrite)")
    args = parser.parse_args()
    request = ExternalContextRequest.model_validate_json(args.input.read_bytes())
    result = analyze_external_context(request)
    text = result.model_dump_json(indent=2) + "\n"
    if args.output is None:
        print(text, end="")
    else:
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            output.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
