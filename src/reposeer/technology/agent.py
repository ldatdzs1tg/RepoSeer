"""Lightweight heuristic rule agent for textual technology extraction."""

import re
from dataclasses import dataclass
from typing import Literal

from reposeer.technology.registry import TechnologyRegistry, TechSpec

SignalKind = Literal["title", "install_code", "import_code", "badge", "body_mention"]

# Common dictionary words that collide with technology names in casual English prose
AMBIGUOUS_WORDS = {
    "requests",
    "click",
    "rich",
    "black",
    "attrs",
    "mock",
    "wheel",
    "build",
    "test",
    "date",
}


@dataclass
class AgentMatch:
    """A verified technology mention extracted by the agent."""

    spec: TechSpec
    matched_text: str
    confidence: float
    signal_kind: SignalKind
    context_snippet: str


class LightweightTechAgent:
    """Heuristic rule-guided agent analyzing README content and prose descriptions."""

    def __init__(self, registry: TechnologyRegistry | None = None):
        self.registry = registry or TechnologyRegistry()

    def analyze_text(
        self,
        text: str,
        *,
        repo_name: str | None = None,
        min_confidence: float = 0.5,
    ) -> list[AgentMatch]:
        """Analyze text and extract technology matches with contextual scoring."""
        if not text or not text.strip():
            return []

        matches: dict[str, AgentMatch] = {}
        lines = text.splitlines()

        for line_idx, line in enumerate(lines):
            line_stripped = line.strip()
            if not line_stripped:
                continue

            # 1. Title / Header signal (# Flask, ## Pydantic)
            if line_stripped.startswith(("#", "===")):
                self._scan_line(
                    line_stripped,
                    signal_kind="title",
                    base_confidence=0.95,
                    matches=matches,
                    repo_name=repo_name,
                )
                continue

            # 2. Install / CLI command signal (pip install ..., uv add ...)
            if re.search(r"\b(?:pip\s+install|uv\s+add|poetry\s+add|conda\s+install)\s+([A-Za-z0-9_-]+)", line_stripped, re.I):
                self._scan_line(
                    line_stripped,
                    signal_kind="install_code",
                    base_confidence=0.92,
                    matches=matches,
                    repo_name=repo_name,
                )
                continue

            # 3. Import statements (import flask, from pydantic import ...)
            if re.search(r"\b(?:import\s+|from\s+)([A-Za-z0-9_]+)", line_stripped):
                self._scan_line(
                    line_stripped,
                    signal_kind="import_code",
                    base_confidence=0.90,
                    matches=matches,
                    repo_name=repo_name,
                )
                continue

            # 4. Badges / Links (shields.io, pypi.org/project/...)
            if "shields.io" in line_stripped or "pypi.org/project" in line_stripped:
                self._scan_line(
                    line_stripped,
                    signal_kind="badge",
                    base_confidence=0.88,
                    matches=matches,
                    repo_name=repo_name,
                )
                continue

            # 5. General body mentions
            self._scan_line(
                line_stripped,
                signal_kind="body_mention",
                base_confidence=0.70,
                matches=matches,
                repo_name=repo_name,
            )

        # Filter by minimum confidence
        return [m for m in matches.values() if m.confidence >= min_confidence]

    def _scan_line(
        self,
        line: str,
        signal_kind: SignalKind,
        base_confidence: float,
        matches: dict[str, AgentMatch],
        repo_name: str | None,
    ) -> None:
        """Scan a line against all registered technology keywords and aliases."""
        for spec in self.registry.all_specs():
            candidate_tokens = [spec.name, spec.slug, *spec.aliases, *spec.keywords]

            for token in candidate_tokens:
                if len(token) < 2:
                    continue

                # Regex whole word boundary
                pattern = r"\b" + re.escape(token) + r"\b"
                match = re.search(pattern, line, re.IGNORECASE)
                if not match:
                    continue

                matched_word = match.group(0)
                confidence = base_confidence

                # If word is in AMBIGUOUS_WORDS, require exact case match or code context
                token_lower = token.lower()
                if token_lower in AMBIGUOUS_WORDS:
                    if signal_kind == "body_mention":
                        # If matched lowercase in ordinary prose, penalty unless matches repo name
                        if matched_word == token_lower and (not repo_name or repo_name.lower() != token_lower):
                            confidence -= 0.35
                        elif matched_word[0].isupper():
                            # Capitalized in prose (e.g. "Requests is...")
                            confidence -= 0.10

                # Comparative / negative context penalty
                # e.g., "Unlike Flask...", "Migrated from Django...", "Inspired by FastAPI"
                if re.search(r"\b(?:unlike|inspired by|alternative to|migration from|replaces)\s+" + re.escape(token), line, re.IGNORECASE):
                    confidence = min(confidence, 0.45)

                # Boost if matches repository name exactly
                if repo_name and (repo_name.lower() == spec.slug.lower() or repo_name.lower() == spec.name.lower()):
                    confidence = min(1.0, confidence + 0.15)

                # Keep match if highest confidence for this technology
                if spec.slug not in matches or confidence > matches[spec.slug].confidence:
                    snippet = line[:120].strip()
                    matches[spec.slug] = AgentMatch(
                        spec=spec,
                        matched_text=matched_word,
                        confidence=round(confidence, 2),
                        signal_kind=signal_kind,
                        context_snippet=snippet,
                    )
