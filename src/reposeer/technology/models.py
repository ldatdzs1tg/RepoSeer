"""Data models for technology detection and search terms generation."""

from typing import Any, Literal
from pydantic import BaseModel, Field

from reposeer.contracts import TechnologyEntity

SearchIntent = Literal[
    "general",
    "maintenance",
    "announcement",
    "deprecation",
    "security",
    "ecosystem",
    "migration",
]

SearchPlatform = Literal["hackernews", "web", "news", "all"]


class SearchTerm(BaseModel):
    """Structured query for retrieving external information."""

    query: str = Field(description="Search query string formatted for external providers")
    tech_slug: str = Field(description="Associated technology slug")
    intent: SearchIntent = Field(description="Search intent or information category")
    platform: SearchPlatform = Field(default="all", description="Target external provider platform")
    priority: int = Field(default=3, ge=1, le=5, description="Priority weight (1 highest, 5 lowest)")
    keywords: list[str] = Field(default_factory=list, description="Constituent keywords in the query")


class DetectedSignal(BaseModel):
    """Raw signal observed from metadata, manifests, topics, or content."""

    source: Literal[
        "repo_identity",
        "language",
        "topic",
        "dependency",
        "readme",
        "pinned",
        "agent",
    ]
    raw_token: str
    confidence: float = Field(ge=0.0, le=1.0)
    context: str | None = None


class DetectionResult(BaseModel):
    """Result returned by the technology detection module."""

    entities: list[TechnologyEntity] = Field(
        default_factory=list,
        description="Canonical TechnologyEntity instances conforming to schema T-011",
    )
    search_terms: list[SearchTerm] = Field(
        default_factory=list,
        description="Prioritized search terms generated for external information retrieval",
    )
    primary_technology: TechnologyEntity | None = Field(
        default=None,
        description="Primary technology entity associated with the repository/package",
    )
    confidence_by_slug: dict[str, float] = Field(
        default_factory=dict,
        description="Confidence scores keyed by technology slug",
    )
    detection_sources_by_slug: dict[str, list[str]] = Field(
        default_factory=dict,
        description="List of signal sources contributing to each detected technology",
    )
    detected_signals: list[DetectedSignal] = Field(
        default_factory=list,
        description="All individual evidence signals collected during detection",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Operational metadata and statistics",
    )
