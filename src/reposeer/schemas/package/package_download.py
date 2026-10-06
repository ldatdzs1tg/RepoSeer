"""Package download statistics schema."""

from datetime import date

from reposeer.schemas.common import BaseSchema


class PackageDownloadRecord(BaseSchema):
    """Time-series download count for measuring adoption."""

    ecosystem: str
    package_name: str
    date: date
    downloads: int = 0
