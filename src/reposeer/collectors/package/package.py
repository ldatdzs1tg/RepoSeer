"""Package metadata collector."""

from reposeer.clients.pypi import PyPIClient
from reposeer.schemas.package.package import PackageRecord


class PackageCollector:
    def __init__(self, pypi_client: PyPIClient | None = None):
        self.pypi_client = pypi_client or PyPIClient()

    def collect_pypi(self, package_name: str) -> PackageRecord:
        data = self.pypi_client.get_package_info(package_name)
        info = data.get("info", {})
        return PackageRecord(
            ecosystem="pypi",
            name=package_name,
            description=info.get("summary"),
            latest_version=info.get("version"),
            homepage=info.get("home_page"),
            repository_url=info.get("project_urls", {}).get("Source")
            or info.get("project_urls", {}).get("Repository"),
            license=info.get("license"),
        )
