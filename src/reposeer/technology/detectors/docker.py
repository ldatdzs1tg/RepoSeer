"""Dockerfile base image detector."""

from reposeer.schemas.technology.repo_technology import RepoTechnologyAssociation
from reposeer.technology.aliases import canonicalize_technology
from reposeer.technology.detectors.base import BaseDetector


class DockerDetector(BaseDetector):
    """Detects runtime and base image technologies from Dockerfiles."""

    def can_detect(self, filename: str) -> bool:
        return filename.lower() in ("dockerfile", "containerfile") or filename.endswith(
            ".dockerfile"
        )

    def detect(
        self, repo_full_name: str, filename: str, content: str
    ) -> list[RepoTechnologyAssociation]:
        results: list[RepoTechnologyAssociation] = []
        for line in content.splitlines():
            line = line.strip()
            if line.upper().startswith("FROM "):
                parts = line.split()
                if len(parts) >= 2:
                    image = parts[1].split(":")[0].split("/")[-1]
                    canonical = canonicalize_technology(image)
                    results.append(
                        RepoTechnologyAssociation(
                            repo_full_name=repo_full_name,
                            name=canonical,
                            source=filename,
                            confidence=0.85,
                        )
                    )
        return results
