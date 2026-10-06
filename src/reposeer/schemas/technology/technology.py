"""Canonical technology representation schema."""

from reposeer.schemas.common import BaseSchema


class TechnologyEntity(BaseSchema):
    """Canonical technology entity definition."""

    name: str  # Canonical name (e.g. "PyTorch", "React")
    category: str | None = None  # e.g. "Deep Learning", "Frontend Framework"
    ecosystem: str | None = None  # e.g. "python", "javascript"
    description: str | None = None
    website_url: str | None = None
