"""Package distribution artifact contract (wheel, sdist, etc.)."""

from datetime import datetime

from reposeer.schemas.common import BaseSchema


class PackageDistributionRecord(BaseSchema):
    """Distribution artifact file published for a release."""

    ecosystem: str
    package_name: str
    version: str
    filename: str
    packagetype: str | None = None  # bdist_wheel, sdist, etc.
    size_bytes: int = 0
    upload_time: datetime | None = None
    sha256: str | None = None
