"""Python dependency detector (requirements.txt, pyproject.toml)."""

import re

from reposeer.schemas.technology.repo_technology import RepoTechnologyAssociation
from reposeer.technology.aliases import canonicalize_technology
from reposeer.technology.detectors.base import BaseDetector


class PythonDetector(BaseDetector):
    """Detects Python dependencies from standard manifest files."""

    def can_detect(self, filename: str) -> bool:
        return filename in ("requirements.txt", "pyproject.toml", "setup.py")

    def detect(
        self, repo_full_name: str, filename: str, content: str
    ) -> list[RepoTechnologyAssociation]:
        results: list[RepoTechnologyAssociation] = []
        seen = set()

        if filename == "requirements.txt":
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                match = re.match(r"^([a-zA-Z0-9_\-\.]+)", line)
                if match:
                    pkg = match.group(1).lower()
                    canonical = canonicalize_technology(pkg)
                    if canonical not in seen:
                        seen.add(canonical)
                        results.append(
                            RepoTechnologyAssociation(
                                repo_full_name=repo_full_name,
                                name=canonical,
                                source=filename,
                                confidence=1.0,
                            )
                        )
        elif filename == "pyproject.toml":
            # Simple regex search for dependencies in toml
            matches = re.findall(r'["\']([a-zA-Z0-9_\-\.]+)(?:[><=~^].*)?["\']', content)
            for m in matches:
                canonical = canonicalize_technology(m.lower())
                if canonical not in seen and len(canonical) > 1:
                    seen.add(canonical)
                    results.append(
                        RepoTechnologyAssociation(
                            repo_full_name=repo_full_name,
                            name=canonical,
                            source=filename,
                            confidence=0.9,
                        )
                    )

        return results
