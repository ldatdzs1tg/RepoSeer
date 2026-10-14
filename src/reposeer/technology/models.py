"""Data models for technology detection and search terms generation."""

import hashlib
import json
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from reposeer.contracts import TechnologyEntity
from reposeer.contracts.models import Source
from reposeer.technology.registry import TechSpec

SearchIntent = Literal[
    "general",
    "maintenance",
    "announcement",
    "deprecation",
    "security",
    "ecosystem",
    "migration",
    # D04 canonical intents
    "direct_package",
    "functional_demand",
    "alternatives",
    "migration_adoption",
]

SearchPlatform = Literal[
    "hackernews",
    "web",
    "news",
    "stackexchange",
    "devto",
    "all",
]


class SearchTerm(BaseModel):
    """Structured query for retrieving external information."""

    query: str = Field(description="Search query string formatted for external providers")
    tech_slug: str | None = Field(default=None, description="Catalog slug; null for verified subject-only queries")
    intent: SearchIntent = Field(description="Search intent or information category")
    platform: SearchPlatform = Field(default="all", description="Target external provider platform")
    priority: int = Field(default=3, ge=1, le=5, description="Priority weight (1 highest, 5 lowest)")
    keywords: list[str] = Field(default_factory=list, description="Constituent keywords in the query")
    query_id: str | None = Field(default=None, description="Deterministic ID computed from platform, intent, and normalized query")
    context_level: Literal["package_specific", "function_level", "alternative_level"] = Field(
        default="package_specific", description="Context level of the search query"
    )
    query_version: str = Field(default="1.0.0", description="Query generation policy version")

    @model_validator(mode="after")
    def populate_query_id(self) -> "SearchTerm":
        if self.query_id is None:
            norm_query = " ".join(self.query.strip().split()).casefold()
            raw = f"{self.platform}:{self.intent}:{norm_query}"
            self.query_id = f"query:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
        return self


def compute_query_set_hash(terms: Sequence[SearchTerm]) -> str:
    """Compute 64-char lowercase hex SHA-256 of canonical JSON of sorted (platform, intent, context_level, query_version, normalized_query) tuples."""
    tuples = []
    for t in terms:
        norm_q = " ".join(t.query.strip().split()).casefold()
        tuples.append((
            t.platform,
            t.intent,
            t.context_level,
            t.query_version,
            norm_q,
        ))
    tuples.sort()
    canonical_json = json.dumps(tuples, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class SearchPlan(BaseModel):
    """Execution plan of external search terms for a repository/package."""

    package_id: str | None = None
    repo_id: str | None = None
    repo_full_name: str | None = None
    profile_status: Literal["verified", "unavailable", "not_provided"] = "not_provided"
    query_version: str = "1.0.0"
    query_set_hash: str = Field(
        default="",
        description="64-char lowercase hex SHA-256 of canonical JSON of sorted (platform, intent, context_level, query_version, normalized_query) tuples",
    )
    terms: list[SearchTerm] = Field(default_factory=list)

    @model_validator(mode="after")
    def populate_query_set_hash(self) -> "SearchPlan":
        if not self.query_set_hash:
            self.query_set_hash = compute_query_set_hash(self.terms)
        return self



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
    technology_id: str | None = None
    repo_id: str | None = None
    repo_full_name: str | None = None
    relationship: Literal["implements", "depends_on", "optional_dependency", "dev_dependency", "mentions", "language", "topic", "pinned"] = "mentions"
    dependency_scope: str | None = None
    source_url: str | None = None
    raw_record_ids: list[str] = Field(default_factory=list)
    observation_source: Source | None = None
    retrieved_at: str | None = None
    available_at: str | None = None


class DetectionResult(BaseModel):
    """Result returned by the technology detection module."""

    resolved_specs: list[TechSpec] = Field(default_factory=list)
    primary_spec: TechSpec | None = None
    diagnostics: list[str] = Field(default_factory=list)
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
