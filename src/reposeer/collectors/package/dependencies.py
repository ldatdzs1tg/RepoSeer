"""Package dependency collector."""

from reposeer.schemas.package.dependency import DependencyRecord


class PackageDependencyCollector:
    def collect(self, package_name: str) -> list[DependencyRecord]:
        return []
