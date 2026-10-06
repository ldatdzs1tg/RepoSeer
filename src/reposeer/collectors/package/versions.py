"""Package versions collector."""

from reposeer.clients.pypi import PyPIClient
from reposeer.schemas.package.package_version import PackageVersionRecord


class PackageVersionCollector:
    def __init__(self, pypi_client: PyPIClient | None = None):
        self.pypi_client = pypi_client or PyPIClient()

    def collect(self, package_name: str) -> list[PackageVersionRecord]:
        data = self.pypi_client.get_package_info(package_name)
        releases = data.get("releases", {})
        records = []
        for ver in releases:
            records.append(
                PackageVersionRecord(
                    ecosystem="pypi",
                    package_name=package_name,
                    version=ver,
                )
            )
        return records
