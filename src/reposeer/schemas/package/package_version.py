"""Package release version contract."""

from datetime import datetime

from reposeer.schemas.common import BaseSchema


class PackageVersionRecord(BaseSchema):
    """Individual version release of an ecosystem package."""

    ecosystem: str
    package_name: str
    version: str
    published_at: datetime | None = None
    is_yanked: bool = False
