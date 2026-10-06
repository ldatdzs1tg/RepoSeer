"""Direct and transitive package dependency contract."""

from reposeer.schemas.common import BaseSchema


class DependencyRecord(BaseSchema):
    """Declared dependency of a package or repository."""

    source_entity: str  # e.g. "repo:pytorch/pytorch" or "pkg:torch"
    ecosystem: str
    dependency_name: str
    version_constraint: str | None = None
    is_optional: bool = False
    scope: str = "runtime"  # "runtime", "dev", "test"
