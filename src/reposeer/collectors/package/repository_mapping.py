"""Package to repository mapping collector."""

from reposeer.schemas.package.package_repository import PackageRepositoryMapping


class PackageRepositoryResolver:
    def map_package(
        self, ecosystem: str, package_name: str, repo_url: str
    ) -> PackageRepositoryMapping:
        parts = repo_url.rstrip("/").split("/")
        full_name = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else package_name
        return PackageRepositoryMapping(
            ecosystem=ecosystem,
            package_name=package_name,
            repo_full_name=full_name,
            repository_url=repo_url,
            confidence=1.0,
        )
