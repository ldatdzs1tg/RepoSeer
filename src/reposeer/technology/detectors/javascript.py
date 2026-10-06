"""JavaScript / Node.js detector (package.json)."""

import json

from reposeer.schemas.technology.repo_technology import RepoTechnologyAssociation
from reposeer.technology.aliases import canonicalize_technology
from reposeer.technology.detectors.base import BaseDetector


class JavaScriptDetector(BaseDetector):
    """Detects technologies from package.json manifests."""

    def can_detect(self, filename: str) -> bool:
        return filename == "package.json"

    def detect(
        self, repo_full_name: str, filename: str, content: str
    ) -> list[RepoTechnologyAssociation]:
        results: list[RepoTechnologyAssociation] = []
        try:
            data = json.loads(content)
            deps = {}
            if "dependencies" in data and isinstance(data["dependencies"], dict):
                deps.update(data["dependencies"])
            if "devDependencies" in data and isinstance(data["devDependencies"], dict):
                deps.update(data["devDependencies"])

            for dep in deps:
                canonical = canonicalize_technology(dep)
                results.append(
                    RepoTechnologyAssociation(
                        repo_full_name=repo_full_name,
                        name=canonical,
                        source=filename,
                        confidence=1.0,
                    )
                )
        except Exception:
            pass
        return results
