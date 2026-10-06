"""Package vulnerabilities collector."""

from reposeer.clients.osv import OSVClient
from reposeer.schemas.package.vulnerability import VulnerabilityRecord


class PackageVulnerabilityCollector:
    def __init__(self, osv_client: OSVClient | None = None):
        self.osv_client = osv_client or OSVClient()

    def collect(self, package_name: str, ecosystem: str = "PyPI") -> list[VulnerabilityRecord]:
        vulns = self.osv_client.query_vulnerabilities(package_name, ecosystem)
        records = []
        for v in vulns:
            records.append(
                VulnerabilityRecord(
                    id=v.get("id", ""),
                    ecosystem=ecosystem.lower(),
                    package_name=package_name,
                    summary=v.get("summary"),
                    aliases=v.get("aliases", []),
                )
            )
        return records
