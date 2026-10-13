"""Lightweight heuristic rule agent for textual technology extraction."""

import ast
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
    """A heuristic text mention; neither verified usage nor lifecycle evidence."""

    spec: TechSpec
    matched_text: str
    confidence: float
    signal_kind: SignalKind
    context_snippet: str


def _imported_modules(line: str) -> list[str]:
    """Read module roots from a single Python import statement, without executing it."""
    statement = line.strip().removeprefix(">>> ").strip()
    if statement.startswith("`") and statement.endswith("`"):
        statement = statement.strip("`").strip()
    if not re.match(r"^(?:import|from)\s+", statement):
        return []
    try:
        body = ast.parse(statement).body
    except SyntaxError:
        return []
    if len(body) != 1:
        return []
    node = body[0]
    if isinstance(node, ast.Import):
        return [alias.name.split(".", 1)[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module.split(".", 1)[0]]
    return []


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

        for line in lines:
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
            if re.search(r"\b(?:pip(?:3)?\s+install|uv\s+add|poetry\s+add|conda\s+install)\s+(?:--?[\w-]+(?:=[^\s]+)?\s+)*[A-Za-z0-9_-]+", line_stripped, re.I):
                self._scan_line(
                    line_stripped,
                    signal_kind="install_code",
                    base_confidence=0.92,
                    matches=matches,
                    repo_name=repo_name,
                )
                continue

            # 3. Import confidence applies only to actual imported module roots.
            imported_modules = _imported_modules(line_stripped)
            if imported_modules:
                for module in imported_modules:
                    self._scan_line(
                        module,
                        signal_kind="import_code",
                        base_confidence=0.90,
                        matches=matches,
                        repo_name=repo_name,
                        context_line=line_stripped,
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
        return [matches[slug] for slug in sorted(matches) if matches[slug].confidence >= min_confidence]

    def _scan_line(
        self,
        line: str,
        signal_kind: SignalKind,
        base_confidence: float,
        matches: dict[str, AgentMatch],
        repo_name: str | None,
        context_line: str | None = None,
    ) -> None:
        """Scan a line against all registered technology keywords and aliases."""
        for spec in self.registry.all_specs():
            candidate_tokens = [spec.name, spec.slug, *spec.aliases]

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

                token_lower = token.casefold()
                if token_lower in AMBIGUOUS_WORDS and signal_kind == "body_mention":
                    # A common word needs explicit package/library context even when capitalized.
                    package_context = re.search(
                        rf"(?:\b{re.escape(token)}\s+(?:Python\s+)?(?:library|package|framework|toolkit)\b"
                        rf"|\b(?:library|package|framework|toolkit)\s+{re.escape(token)}\b)", line, re.I,
                    )
                    if not package_context:
                        confidence = min(confidence, 0.35)
                # Single-letter and ambiguous abbreviations never identify a prose technology.
                if token_lower in {"py", "js", "ts", "uv"} and signal_kind == "body_mention":
                    confidence = min(confidence, 0.35)
                # Suppress comparisons, negatives, alternatives and historical/migration references.
                negative_prefix = (
                    r"(?:unlike|inspired by|alternative to|compared (?:to|with)|"
                    r"(?:migrat(?:ed|ing|ion)|moved|switched) (?:away )?from|"
                    r"previously (?:used|using)|formerly (?:used|using)|used to use|"
                    r"replaces|replaced|instead of|without|no longer (?:uses?|supports?)|"
                    r"(?:does|do)(?:n't| not) (?:use|require|support)|not (?:using|based on))"
                )
                # Apply negative semantics to the concept, so another alias cannot undo them.
                for name in (spec.name, spec.slug, *spec.aliases):
                    escaped_name = re.escape(name)
                    if re.search(
                        negative_prefix + r"\s+(?:the\s+)?(?:[\w.-]+\s*(?:,\s*|and\s+|or\s+))*"
                        + escaped_name + r"\b", line, re.I,
                    ):
                        confidence = min(confidence, 0.40)
                    if re.search(r"\b" + escaped_name + r"\s+(?:vs\.?|versus)\b", line, re.I) or re.search(
                        r"\b(?:vs\.?|versus)\s+" + escaped_name + r"\b", line, re.I,
                    ):
                        confidence = min(confidence, 0.40)
                if token_lower in AMBIGUOUS_WORDS and signal_kind == "title":
                    title = line.lstrip("#= ").strip()
                    if not re.match(re.escape(token) + r"(?:$|\s+(?:Python|library|package|framework|toolkit)\b)", title, re.I):
                        confidence = min(confidence, 0.35)

                # Keep match if highest confidence for this technology
                if spec.slug not in matches or confidence > matches[spec.slug].confidence:
                    snippet = (context_line or line)[:240].strip()
                    matches[spec.slug] = AgentMatch(
                        spec=spec,
                        matched_text=matched_word,
                        confidence=round(confidence, 2),
                        signal_kind=signal_kind,
                        context_snippet=snippet,
                    )
