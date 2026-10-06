"""Technology alias dictionary and canonicalization mapping."""

# Mapping from lowercase token to canonical representation
ALIAS_MAP: dict[str, str] = {
    # Python
    "pytorch": "PyTorch",
    "torch": "PyTorch",
    "torchvision": "PyTorch",
    "tensorflow": "TensorFlow",
    "tf": "TensorFlow",
    "fastapi": "FastAPI",
    "django": "Django",
    "flask": "Flask",
    "scikit-learn": "scikit-learn",
    "sklearn": "scikit-learn",
    "polars": "Polars",
    "pandas": "Pandas",
    "numpy": "NumPy",
    "duckdb": "DuckDB",
    "httpx": "HTTPX",
    "requests": "Requests",
    "pydantic": "Pydantic",
    "langchain": "LangChain",
    "langgraph": "LangGraph",
    # JavaScript / TypeScript
    "react": "React",
    "reactjs": "React",
    "react.js": "React",
    "next": "Next.js",
    "nextjs": "Next.js",
    "next.js": "Next.js",
    "vue": "Vue.js",
    "vuejs": "Vue.js",
    "svelte": "Svelte",
    "express": "Express",
    "typescript": "TypeScript",
    "tailwind": "Tailwind CSS",
    "tailwindcss": "Tailwind CSS",
    # Systems & Cloud
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "redis": "Redis",
}


def canonicalize_technology(raw_name: str) -> str:
    """Normalize raw technology name or dependency string to its canonical form."""
    clean = raw_name.strip().lower()
    return ALIAS_MAP.get(clean, raw_name.strip())
