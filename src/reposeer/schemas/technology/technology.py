"""Legacy internal technology DTO; canonical entities live in reposeer.contracts."""

from reposeer.schemas.common import BaseSchema


class TechnologyEntity(BaseSchema):
    """Legacy metadata DTO, outside the T-011 public exchange contract."""

    name: str  # Canonical name (e.g. "PyTorch", "React")
    category: str | None = None  # e.g. "Deep Learning", "Frontend Framework"
    ecosystem: str | None = None  # e.g. "python", "javascript"
    description: str | None = None
    website_url: str | None = None
