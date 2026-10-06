"""Mapping between raw detected tokens and canonical technology names."""

from reposeer.schemas.common import BaseSchema


class TechnologyAlias(BaseSchema):
    """Alias translation rule."""

    raw_token: str  # e.g. "pytorch", "torch"
    canonical_name: str  # e.g. "PyTorch"
    ecosystem: str | None = None
