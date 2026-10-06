"""Package distribution files collector."""

from reposeer.schemas.package.package_distribution import PackageDistributionRecord


class PackageDistributionCollector:
    def collect(self, package_name: str) -> list[PackageDistributionRecord]:
        return []
