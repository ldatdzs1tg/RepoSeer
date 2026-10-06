"""Package metadata contract."""

from reposeer.schemas.common import BaseSchema, TimestampMixin


class PackageRecord(BaseSchema, TimestampMixin):
    """Metadata representing an open source package in an ecosystem."""

    ecosystem: str  # "pypi", "npm", "crates", etc.
    name: str
    description: str | None = None
    latest_version: str | None = None
    homepage: str | None = None
    repository_url: str | None = None
    license: str | None = None
