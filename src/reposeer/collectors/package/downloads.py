"""Package downloads collector."""

from reposeer.schemas.package.package_download import PackageDownloadRecord


class PackageDownloadCollector:
    def collect(self, package_name: str) -> list[PackageDownloadRecord]:
        return []
