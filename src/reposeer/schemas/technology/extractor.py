import re
from typing import List, Optional
from pydantic import BaseModel, Field

# ------------------------------------------------------------------------------
# Schema T-011
# ------------------------------------------------------------------------------
class TechnologyEntity(BaseModel):
    """
    Schema T-011: Technology Entity
    Represents a technology extracted from a repository's metadata.
    """
    canonical_name: str = Field(
        ..., 
        description="The canonical name of the technology (e.g., 'React', 'PostgreSQL', 'Python')."
    )
    aliases: List[str] = Field(
        default_factory=list, 
        description="Alternative names, keywords, or abbreviations."
    )
    category: str = Field(
        ..., 
        description="Broad category of the technology (e.g., 'framework', 'database', 'language')."
    )
    search_terms: List[str] = Field(
        default_factory=list, 
        description="Recommended search terms to find external information about this technology."
    )

class RepoMetadata(BaseModel):
    name: str
    readme_content: Optional[str] = ""
    topics: List[str] = Field(default_factory=list)
    dependencies: List[str] = Field(default_factory=list)


# ------------------------------------------------------------------------------
# Module Logic (Rules + Parsing + Light Agent Placeholder)
# ------------------------------------------------------------------------------

# Pre-defined mapping for standard technologies
TECH_RULES = {
    "react": {
        "canonical": "React",
        "category": "frontend-framework",
        "aliases": ["reactjs", "react.js"],
        "search_terms": ["React library documentation", "React frontend framework tutorials"]
    },
    "vue": {
        "canonical": "Vue.js",
        "category": "frontend-framework",
        "aliases": ["vue", "vuejs"],
        "search_terms": ["Vue.js framework", "Vue JS guide"]
    },
    "postgres": {
        "canonical": "PostgreSQL",
        "category": "database",
        "aliases": ["postgresql", "pg", "postgres"],
        "search_terms": ["PostgreSQL database architecture", "PostgreSQL open source docs"]
    },
    "python": {
        "canonical": "Python",
        "category": "language",
        "aliases": ["py", "python3"],
        "search_terms": ["Python programming language usage", "Python official docs"]
    },
    "pydantic": {
        "canonical": "Pydantic",
        "category": "library",
        "aliases": ["pydantic-v2"],
        "search_terms": ["Pydantic python library data validation", "Pydantic V2 usage"]
    },
    "duckdb": {
        "canonical": "DuckDB",
        "category": "database",
        "aliases": ["duck-db"],
        "search_terms": ["DuckDB embedded database analytics", "DuckDB python API"]
    },
    "polars": {
        "canonical": "Polars",
        "category": "library",
        "aliases": ["python-polars", "polars-rs"],
        "search_terms": ["Polars dataframe python", "Polars data processing"]
    }
}

class TechExtractor:
    def __init__(self, use_llm: bool = False):
        self.use_llm = use_llm
        # For a light agent, we could integrate an LLM client here
        # self.llm_client = LightAgentClient() if use_llm else None

    def _generate_search_terms_for_unknown(self, name: str) -> List[str]:
        return [
            f"{name} technology documentation",
            f"{name} open source project",
            f"{name} programming usage"
        ]

    def extract(self, metadata: RepoMetadata) -> List[TechnologyEntity]:
        """
        Extract technology entities from repo metadata.
        Uses rule-based mapping, dependency checking, and README parsing.
        """
        entities = {}
        
        # 1. Parse from topics
        for topic in metadata.topics:
            topic_lower = topic.lower()
            if topic_lower in TECH_RULES:
                rule = TECH_RULES[topic_lower]
                entities[rule["canonical"]] = TechnologyEntity(
                    canonical_name=rule["canonical"],
                    aliases=rule["aliases"],
                    category=rule["category"],
                    search_terms=rule["search_terms"]
                )
            else:
                # Basic heuristic for unknown topics
                entities[topic] = TechnologyEntity(
                    canonical_name=topic.capitalize(),
                    aliases=[topic],
                    category="unknown",
                    search_terms=self._generate_search_terms_for_unknown(topic)
                )
                
        # 2. Parse from dependencies
        for dep in metadata.dependencies:
            dep_lower = dep.lower()
            if dep_lower in TECH_RULES and TECH_RULES[dep_lower]["canonical"] not in entities:
                rule = TECH_RULES[dep_lower]
                entities[rule["canonical"]] = TechnologyEntity(
                    canonical_name=rule["canonical"],
                    aliases=rule["aliases"],
                    category=rule["category"],
                    search_terms=rule["search_terms"]
                )
            elif dep_lower not in entities and dep_lower not in [k.lower() for k in entities.keys()]:
                entities[dep_lower] = TechnologyEntity(
                    canonical_name=dep.capitalize(),
                    aliases=[dep],
                    category="library/dependency",
                    search_terms=self._generate_search_terms_for_unknown(dep)
                )
        
        # 3. Parse from README (simple regex search for known tech)
        if metadata.readme_content:
            text = metadata.readme_content.lower()
            for key, rule in TECH_RULES.items():
                if rule["canonical"] not in entities:
                    # Look for canonical name or aliases as whole words
                    patterns = [key] + rule["aliases"]
                    for pattern in patterns:
                        # Escape pattern for safe regex
                        safe_pattern = re.escape(pattern)
                        if re.search(rf'\b{safe_pattern}\b', text):
                            entities[rule["canonical"]] = TechnologyEntity(
                                canonical_name=rule["canonical"],
                                aliases=rule["aliases"],
                                category=rule["category"],
                                search_terms=rule["search_terms"]
                            )
                            break
                            
        # 4. Optional Light Agent step to refine "unknown" categories or find implicit tech
        if self.use_llm:
            # entities = self.llm_client.refine_entities(entities, metadata.readme_content)
            pass
        
        return list(entities.values())

# ------------------------------------------------------------------------------
# Usage example for a sample repo
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    sample_repo = RepoMetadata(
        name="reposeer-example",
        topics=["python", "postgres", "data-engineering", "custom-tool"],
        dependencies=["pydantic", "polars", "httpx"],
        readme_content="RepoSeer is an intelligence pipeline. Built on top of Python, DuckDB, and Polars. It utilizes httpx for client requests."
    )
    
    extractor = TechExtractor()
    extracted_tech = extractor.extract(sample_repo)
    
    print("Extracted Technology Entities (Schema T-011):\n")
    for tech in extracted_tech:
        print(tech.model_dump_json(indent=2))
