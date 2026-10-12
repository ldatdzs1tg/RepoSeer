"""Technology detection, canonical entity synthesis, and external search terms generation."""

from reposeer.technology.agent import AgentMatch, LightweightTechAgent
from reposeer.technology.detector import TechnologyDetector, detect_technologies
from reposeer.technology.manifest_parser import (
    parse_manifest_dependencies,
    parse_pyproject_toml,
    parse_requirements_txt,
)
from reposeer.technology.models import (
    DetectedSignal,
    DetectionResult,
    SearchIntent,
    SearchPlatform,
    SearchTerm,
)
from reposeer.technology.registry import TechSpec, TechnologyRegistry
from reposeer.technology.search_terms import SearchTermGenerator

__all__ = [
    "TechnologyDetector",
    "detect_technologies",
    "TechnologyRegistry",
    "TechSpec",
    "LightweightTechAgent",
    "AgentMatch",
    "SearchTermGenerator",
    "SearchTerm",
    "SearchIntent",
    "SearchPlatform",
    "DetectedSignal",
    "DetectionResult",
    "parse_requirements_txt",
    "parse_pyproject_toml",
    "parse_manifest_dependencies",
]
