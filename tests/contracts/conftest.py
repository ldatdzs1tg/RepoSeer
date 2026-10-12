"""Synthetic fixture access; default tests never use live evidence."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def example():
    def load(name):
        return json.loads(
            (ROOT / "examples/contracts" / (name + ".json")).read_text(encoding="utf-8")
        )

    return load
