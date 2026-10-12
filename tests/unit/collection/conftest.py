"""Deterministic clocks for retry, rate-limit, and cache tests."""

from dataclasses import dataclass, field

import pytest


@dataclass
class FakeClock:
    now: float = 1_700_000_000.0
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()
