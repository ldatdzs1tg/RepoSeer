"""Validate three synthetic D07 request/response pairs and bounded review."""

import json
from pathlib import Path

from reposeer.contracts.specialist_agents import (
    REQUEST_ADAPTER,
    RESPONSE_ADAPTER,
    validate_agent_pair,
    validate_conflict_reviews,
)

ROOT = Path(__file__).resolve().parents[1]


def validate_examples(directory: Path) -> int:
    kinds = ("maintenance", "functional", "alternatives")
    for kind in kinds:
        request = REQUEST_ADAPTER.validate_json((directory / f"{kind}_request.json").read_bytes())
        response = RESPONSE_ADAPTER.validate_json((directory / f"{kind}_response.json").read_bytes())
        validate_agent_pair(request, response)
    validate_conflict_reviews([json.loads((directory / "conflict_review.json").read_bytes())])
    return len(kinds)


def main() -> int:
    try:
        count = validate_examples(ROOT / "examples/agents")
    except (ValueError, TypeError, OSError) as error:
        print(f"Invalid specialist examples: {error}")
        return 1
    print(f"Validated {count} specialist request/response pairs and one conflict review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
