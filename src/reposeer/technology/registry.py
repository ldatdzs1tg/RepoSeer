"""Technology catalog registry and canonical specification database."""

from dataclasses import dataclass
from typing import Literal

from reposeer.contracts import Provenance, RawRecord, TechnologyEntity, validate_record
from reposeer.contracts.identity import (
    canonical_github_url,
    normalize_pypi_name,
    package_id,
    repo_id,
    technology_id,
)
from reposeer.contracts.models import SCHEMA_VERSION


def _raw_repository_identity(raw: RawRecord) -> tuple[str, str]:
    """Check native GitHub identity as well as the canonical raw envelope."""
    if raw.source not in {"github_rest", "github_graphql"} or raw.source_record_type != "github_repository":
        raise ValueError("Repository evidence requires a GitHub repository observation")
    numeric = raw.payload.get("id")
    numeric_id = str(numeric) if type(numeric) in {int, str} else None
    canonical_id = repo_id(numeric_id)
    if canonical_id != raw.repo_id:
        raise ValueError("Raw repository ID disagrees with payload numeric ID")
    full_name = raw.payload.get("full_name")
    canonical_github_url(full_name)
    for key in ("language", "primary_language", "description"):
        value = raw.payload.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Raw repository {key} must be text or null")
    topics = raw.payload.get("topics")
    if topics is not None and (not isinstance(topics, list) or not all(isinstance(topic, str) for topic in topics)):
        raise ValueError("Raw repository topics must be a list of text values")
    alternate_name = raw.payload.get("repo_full_name")
    if alternate_name is not None and canonical_github_url(alternate_name) != canonical_github_url(full_name):
        raise ValueError("Raw repository full names disagree")
    if raw.source_record_id is not None and (
        raw.source_record_id.isdecimal() or raw.source_record_id.startswith("github:repository:")
    ) and raw.source_record_id not in {numeric_id, canonical_id}:
        raise ValueError("Raw repository source_record_id disagrees with payload numeric ID")
    return canonical_id, full_name


CategoryType = Literal[
    "language",
    "framework",
    "library",
    "runtime",
    "database",
    "developer_tool",
    "platform",
    "standard",
]
LifecycleType = Literal["active", "deprecated", "end_of_life", "unknown"]


@dataclass(frozen=True)
class TechSpec:
    """Immutable specification for a known technology concept."""

    slug: str
    name: str
    category: CategoryType
    aliases: tuple[str, ...] = ()
    description: str | None = None
    official_urls: tuple[str, ...] = ()
    default_packages: tuple[str, ...] = ()
    known_repo_slugs: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    lifecycle_status: LifecycleType = "active"
    status_evidence_url: str | None = None

    def __post_init__(self) -> None:
        # Accept existing list-based construction without retaining mutable catalog collections.
        for name in ("aliases", "official_urls", "default_packages", "known_repo_slugs", "keywords"):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    def to_contract_entity(
        self,
        *,
        repo_numeric_ids: list[str] | None = None,
        package_names: list[str] | None = None,
        retrieved_at: str | None = None,
        source_url: str | None = None,
        raw_record_id: str | None = None,
        raw_record: RawRecord | None = None,
        provenance: Provenance | None = None,
    ) -> TechnologyEntity:
        """Build from matching catalog/upstream/metadata evidence; provenance alone is insufficient.

        Documentation evidence needs the detector's content/signal/subject checks.
        """
        if raw_record is None:
            raise ValueError("Canonical output requires semantically validated recorded evidence: raw_record")
        raw_record = validate_record(raw_record)
        if not isinstance(raw_record, RawRecord):
            raise TypeError("raw_record must be a canonical RawRecord")
        upstream_repo_id = None
        if raw_record.source == "technology_catalog":
            self._validate_catalog_observation(raw_record)
        elif raw_record.source in {"github_rest", "github_graphql"}:
            observed_id, full_name = _raw_repository_identity(raw_record)
            registry = TechnologyRegistry([self])
            if registry.lookup_by_repo(full_name) == self:
                upstream_repo_id = observed_id
            else:
                language = raw_record.payload.get("language")
                topics = raw_record.payload.get("topics") or []
                language_match = (isinstance(language, str) and self.category == "language"
                                  and registry.lookup(language) == self)
                topic_match = (isinstance(topics, list) and any(
                    isinstance(topic, str) and registry.lookup_by_topic(topic) == self for topic in topics
                ))
                if not language_match and not topic_match:
                    raise ValueError("Repository observation does not identify this technology")
        else:
            raise ValueError("Documentation evidence requires detector-validated content and subject signals")
        observed = Provenance(
            source=raw_record.source, source_url=raw_record.source_url,
            retrieved_at=raw_record.retrieved_at, valid_at=raw_record.retrieved_at,
            available_at=raw_record.available_at, temporal_basis="observation",
            raw_record_ids=[raw_record.raw_record_id], missing_fields=[],
        )
        if provenance is not None and provenance != observed:
            raise ValueError("raw_record and provenance disagree")
        for supplied, actual, label in (
            (retrieved_at, observed.retrieved_at, "retrieved_at"),
            (source_url, observed.source_url, "source_url"),
        ):
            if supplied is not None and supplied != actual:
                raise ValueError(f"{label} disagrees with recorded evidence")
        if raw_record_id is not None and raw_record_id not in observed.raw_record_ids:
            raise ValueError("raw_record_id disagrees with recorded evidence")
        if any(repo_id(str(numeric)) != upstream_repo_id for numeric in (repo_numeric_ids or [])):
            raise ValueError("Repository association requires matching recorded upstream evidence")
        return self._from_verified_provenance(observed, repo_numeric_ids=repo_numeric_ids,
                                              package_names=package_names)

    def _validate_catalog_observation(self, raw: RawRecord) -> None:
        identities = [raw.payload[key] for key in ("slug", "id", "technology_id") if key in raw.payload]
        if (raw.source != "technology_catalog" or raw.source_record_type != "technology_entity"
                or not identities or any(value not in (self.slug, technology_id(self.slug)) for value in identities)
                or ("slug" in raw.payload and raw.payload["slug"] != self.slug)):
            raise ValueError("Catalog observation does not identify this technology")
        source_identity = raw.source_record_id
        # Canonical IDs and known catalog slugs carry technology identity; opaque keys do not.
        if source_identity is not None and (
            source_identity.startswith("tech:") or source_identity == self.slug
            or any(source_identity == spec.slug for spec in DEFAULT_TAXONOMY)
        ) and source_identity not in {self.slug, technology_id(self.slug)}:
            raise ValueError("Catalog source_record_id contradicts technology identity")

    def _from_verified_provenance(
        self, provenance: Provenance, *, repo_numeric_ids: list[str] | None = None,
        package_names: list[str] | None = None,
    ) -> TechnologyEntity:
        """Internal construction after public raw checks or detector-bound semantic evidence.

        The detector must bind provenance to the actual subject and detected signal first.
        """
        provenance = Provenance.model_validate(provenance.model_dump(), strict=True)
        if not self.official_urls:
            raise ValueError("Canonical output requires a real official URL")

        # Normalize and deduplicate repo_ids
        target_repo_ids: list[str] = []
        if repo_numeric_ids:
            seen_repos = set()
            for r_id in repo_numeric_ids:
                canonical_r = repo_id(str(r_id))
                if canonical_r not in seen_repos:
                    seen_repos.add(canonical_r)
                    target_repo_ids.append(canonical_r)

        # Normalize and deduplicate package_ids
        all_pkg_names = list(self.default_packages) + (package_names or [])
        target_pkg_ids: list[str] = []
        seen_pkgs = set()
        for p_name in all_pkg_names:
            try:
                canonical_p = package_id(p_name)
                if canonical_p not in seen_pkgs:
                    seen_pkgs.add(canonical_p)
                    target_pkg_ids.append(canonical_p)
            except ValueError:
                continue

        # Case-insensitively deduplicated aliases
        unique_aliases: list[str] = []
        seen_aliases = set()
        for a in self.aliases:
            folded = a.casefold()
            if folded not in seen_aliases and folded != self.name.casefold():
                seen_aliases.add(folded)
                unique_aliases.append(a)

        # Deduplicate official URLs
        urls = list(dict.fromkeys(self.official_urls))

        entity = TechnologyEntity(
            schema_version=SCHEMA_VERSION,
            record_type="technology_entity",
            technology_id=f"tech:{self.slug}",
            slug=self.slug,
            name=self.name,
            category=self.category,
            aliases=unique_aliases,
            description=self.description,
            official_urls=urls,
            repo_ids=target_repo_ids,
            package_ids=target_pkg_ids,
            lifecycle_status=self.lifecycle_status,
            status_evidence_url=self.status_evidence_url,
            provenance=provenance,
        )
        return validate_record(entity)


# Predefined curated taxonomy catalog
DEFAULT_TAXONOMY: list[TechSpec] = [
    # Languages
    TechSpec(
        slug="python",
        name="Python",
        category="language",
        aliases=["CPython", "py"],
        description="High-level programming language emphasizing code readability and versatility.",
        official_urls=["https://www.python.org/"],
        default_packages=[],
        known_repo_slugs=["python/cpython"],
        keywords=["python", "cpython", "python3"],
    ),
    TechSpec(
        slug="rust",
        name="Rust",
        category="language",
        aliases=["rustlang"],
        description="Empowering everyone to build reliable and efficient software.",
        official_urls=["https://www.rust-lang.org/"],
        default_packages=[],
        known_repo_slugs=["rust-lang/rust"],
        keywords=["rust", "cargo", "rustlang"],
    ),
    TechSpec(
        slug="cython",
        name="Cython",
        category="language",
        aliases=["Pyrex"],
        description="Optimising static compiler for both the Python programming language and the extended Cython programming language.",
        official_urls=["https://cython.org/"],
        default_packages=["cython"],
        known_repo_slugs=["cython/cython"],
        keywords=["cython", "pyx"],
    ),
    TechSpec(
        slug="javascript",
        name="JavaScript",
        category="language",
        aliases=["ECMAScript", "JS"],
        description="High-level, interpreted programming language primarily for the Web.",
        official_urls=["https://ecma-international.org/"],
        keywords=["javascript", "js", "ecmascript"],
    ),
    TechSpec(
        slug="typescript",
        name="TypeScript",
        category="language",
        aliases=["TS"],
        description="Typed superset of JavaScript that compiles to plain JavaScript.",
        official_urls=["https://www.typescriptlang.org/"],
        keywords=["typescript", "ts"],
    ),

    # Web & API Frameworks
    TechSpec(
        slug="flask",
        name="Flask",
        category="framework",
        aliases=["Flask-Framework", "pallets-flask"],
        description="The Python micro framework for building web applications.",
        official_urls=["https://palletsprojects.com/p/flask/"],
        default_packages=["flask"],
        known_repo_slugs=["pallets/flask"],
        keywords=["flask", "microframework", "wsgi application"],
    ),
    TechSpec(
        slug="django",
        name="Django",
        category="framework",
        aliases=["Django-Framework", "django-web"],
        description="The web framework for perfectionists with deadlines.",
        official_urls=["https://www.djangoproject.com/"],
        default_packages=["django"],
        known_repo_slugs=["django/django"],
        keywords=["django", "django-orm"],
    ),
    TechSpec(
        slug="fastapi",
        name="FastAPI",
        category="framework",
        aliases=["FastAPI-Framework"],
        description="Modern, fast (high-performance), web framework for building APIs with Python.",
        official_urls=["https://fastapi.tiangolo.com/"],
        default_packages=["fastapi"],
        known_repo_slugs=["fastapi/fastapi", "tiangolo/fastapi"],
        keywords=["fastapi", "asgi api"],
    ),
    TechSpec(
        slug="starlette",
        name="Starlette",
        category="framework",
        aliases=["starlette-framework"],
        description="Lightweight ASGI framework/toolkit, ideal for building async web services in Python.",
        official_urls=["https://www.starlette.io/"],
        default_packages=["starlette"],
        known_repo_slugs=["encode/starlette"],
        keywords=["starlette", "asgi toolkit"],
    ),
    TechSpec(
        slug="tornado",
        name="Tornado",
        category="framework",
        aliases=["tornado-web"],
        description="A Python web framework and asynchronous networking library.",
        official_urls=["https://www.tornadoweb.org/"],
        default_packages=["tornado"],
        known_repo_slugs=["tornadoweb/tornado"],
        keywords=["tornado", "tornadoweb"],
    ),

    # Libraries & Data Validation
    TechSpec(
        slug="pydantic",
        name="Pydantic",
        category="library",
        aliases=["pydantic-v2", "pydantic-core"],
        description="Data validation and settings management using Python type hints.",
        official_urls=["https://docs.pydantic.dev/"],
        default_packages=["pydantic", "pydantic-core"],
        known_repo_slugs=["pydantic/pydantic"],
        keywords=["pydantic", "basemodel", "data validation"],
    ),
    TechSpec(
        slug="requests",
        name="Requests",
        category="library",
        aliases=["requests-http", "python-requests"],
        description="A simple, yet elegant, HTTP library for Python.",
        official_urls=["https://requests.readthedocs.io/"],
        default_packages=["requests"],
        known_repo_slugs=["psf/requests"],
        keywords=["requests", "http library"],
    ),
    TechSpec(
        slug="httpx",
        name="HTTPX",
        category="library",
        aliases=["python-httpx"],
        description="A next-generation HTTP client for Python with sync and async support.",
        official_urls=["https://www.python-httpx.org/"],
        default_packages=["httpx"],
        known_repo_slugs=["encode/httpx"],
        keywords=["httpx", "async http"],
    ),
    TechSpec(
        slug="urllib3",
        name="urllib3",
        category="library",
        aliases=["urllib3-http"],
        description="A powerful, user-friendly HTTP client library for Python.",
        official_urls=["https://urllib3.readthedocs.io/"],
        default_packages=["urllib3"],
        known_repo_slugs=["urllib3/urllib3"],
        keywords=["urllib3", "connection pooling"],
    ),
    TechSpec(
        slug="jinja2",
        name="Jinja2",
        category="library",
        aliases=["jinja", "pallets-jinja"],
        description="A very fast and expressive template engine for Python.",
        official_urls=["https://palletsprojects.com/p/jinja/"],
        default_packages=["jinja2"],
        known_repo_slugs=["pallets/jinja"],
        keywords=["jinja", "jinja2", "template engine"],
    ),
    TechSpec(
        slug="werkzeug",
        name="Werkzeug",
        category="library",
        aliases=["pallets-werkzeug"],
        description="A comprehensive WSGI web application library.",
        official_urls=["https://palletsprojects.com/p/werkzeug/"],
        default_packages=["werkzeug"],
        known_repo_slugs=["pallets/werkzeug"],
        keywords=["werkzeug", "wsgi toolkit"],
    ),
    TechSpec(
        slug="sqlalchemy",
        name="SQLAlchemy",
        category="library",
        aliases=["sqlalchemy-orm"],
        description="The Python SQL Toolkit and Object Relational Mapper.",
        official_urls=["https://www.sqlalchemy.org/"],
        default_packages=["sqlalchemy"],
        known_repo_slugs=["sqlalchemy/sqlalchemy"],
        keywords=["sqlalchemy", "orm", "sql toolkit"],
    ),
    TechSpec(
        slug="polars",
        name="Polars",
        category="library",
        aliases=["polars-rs", "py-polars"],
        description="Fast multi-threaded DataFrame library in Rust and Python.",
        official_urls=["https://pola.rs/"],
        default_packages=["polars"],
        known_repo_slugs=["pola-rs/polars"],
        keywords=["polars", "dataframes"],
    ),
    TechSpec(
        slug="pandas",
        name="Pandas",
        category="library",
        aliases=["pandas-dev"],
        description="Flexible and powerful data analysis / manipulation library for Python.",
        official_urls=["https://pandas.pydata.org/"],
        default_packages=["pandas"],
        known_repo_slugs=["pandas-dev/pandas"],
        keywords=["pandas", "dataframe analysis"],
    ),
    TechSpec(
        slug="numpy",
        name="NumPy",
        category="library",
        aliases=["numpy-array"],
        description="The fundamental package for scientific computing with Python.",
        official_urls=["https://numpy.org/"],
        default_packages=["numpy"],
        known_repo_slugs=["numpy/numpy"],
        keywords=["numpy", "multidimensional array"],
    ),
    TechSpec(
        slug="scipy",
        name="SciPy",
        category="library",
        aliases=["scipy-org"],
        description="Fundamental algorithms for scientific computing in Python.",
        official_urls=["https://scipy.org/"],
        default_packages=["scipy"],
        known_repo_slugs=["scipy/scipy"],
        keywords=["scipy", "scientific computing"],
    ),
    TechSpec(
        slug="scikit-learn",
        name="scikit-learn",
        category="library",
        aliases=["sklearn"],
        description="Machine learning in Python.",
        official_urls=["https://scikit-learn.org/"],
        default_packages=["scikit-learn"],
        known_repo_slugs=["scikit-learn/scikit-learn"],
        keywords=["scikit-learn", "sklearn", "machine learning"],
    ),
    TechSpec(
        slug="pytorch",
        name="PyTorch",
        category="framework",
        aliases=["torch"],
        description="Tensors and Dynamic neural networks in Python with strong GPU acceleration.",
        official_urls=["https://pytorch.org/"],
        default_packages=["torch"],
        known_repo_slugs=["pytorch/pytorch"],
        keywords=["pytorch", "torch", "deep learning"],
    ),
    TechSpec(
        slug="trafilatura",
        name="Trafilatura",
        category="library",
        aliases=["trafilatura-scraper"],
        description="Web scraping library and command-line tool to discover and extract text and metadata.",
        official_urls=["https://trafilatura.readthedocs.io/"],
        default_packages=["trafilatura"],
        known_repo_slugs=["adbar/trafilatura"],
        keywords=["trafilatura", "text extraction"],
    ),
    TechSpec(
        slug="click",
        name="Click",
        category="library",
        aliases=["pallets-click"],
        description="Composable command line interface toolkit for Python.",
        official_urls=["https://palletsprojects.com/p/click/"],
        default_packages=["click"],
        known_repo_slugs=["pallets/click"],
        keywords=["click", "cli toolkit"],
    ),
    TechSpec(
        slug="typer",
        name="Typer",
        category="library",
        aliases=["typer-cli"],
        description="Typer, build great CLIs. Easy to code, based on Python type hints.",
        official_urls=["https://typer.tiangolo.com/"],
        default_packages=["typer"],
        known_repo_slugs=["fastapi/typer", "tiangolo/typer"],
        keywords=["typer", "type hint cli"],
    ),
    TechSpec(
        slug="rich",
        name="Rich",
        category="library",
        aliases=["rich-terminal"],
        description="Rich is a Python library for rich text and beautiful formatting in the terminal.",
        official_urls=["https://rich.readthedocs.io/"],
        default_packages=["rich"],
        known_repo_slugs=["textualize/rich"],
        keywords=["rich", "terminal formatting"],
    ),
    TechSpec(
        slug="tenacity",
        name="Tenacity",
        category="library",
        aliases=["tenacity-retry"],
        description="Retrying library for Python.",
        official_urls=["https://tenacity.readthedocs.io/"],
        default_packages=["tenacity"],
        known_repo_slugs=["jd/tenacity"],
        keywords=["tenacity", "exponential backoff"],
    ),

    # Developer Tools & Testing
    TechSpec(
        slug="pytest",
        name="Pytest",
        category="developer_tool",
        aliases=["pytest-framework"],
        description="The pytest framework makes it easy to write small, readable tests.",
        official_urls=["https://pytest.org/"],
        default_packages=["pytest"],
        known_repo_slugs=["pytest-dev/pytest"],
        keywords=["pytest", "unit testing"],
    ),
    TechSpec(
        slug="ruff",
        name="Ruff",
        category="developer_tool",
        aliases=["astral-ruff"],
        description="An extremely fast Python linter and code formatter, written in Rust.",
        official_urls=["https://astral.sh/ruff"],
        default_packages=["ruff"],
        known_repo_slugs=["astral-sh/ruff"],
        keywords=["ruff", "linter", "code formatter"],
    ),
    TechSpec(
        slug="black",
        name="Black",
        category="developer_tool",
        aliases=["psf-black"],
        description="The uncompromising Python code formatter.",
        official_urls=["https://black.readthedocs.io/"],
        default_packages=["black"],
        known_repo_slugs=["psf/black"],
        keywords=["black", "uncompromising formatter"],
    ),
    TechSpec(
        slug="uv",
        name="uv",
        category="developer_tool",
        aliases=["astral-uv"],
        description="An extremely fast Python package and project manager, written in Rust.",
        official_urls=["https://astral.sh/uv"],
        default_packages=["uv"],
        known_repo_slugs=["astral-sh/uv"],
        keywords=["uv", "package manager"],
    ),
    TechSpec(
        slug="docker",
        name="Docker",
        category="developer_tool",
        aliases=["docker-container"],
        description="Container platform for packaging and deploying applications.",
        official_urls=["https://www.docker.com/"],
        keywords=["docker", "container", "dockerfile"],
    ),

    # Databases & Runtimes
    TechSpec(
        slug="duckdb",
        name="DuckDB",
        category="database",
        aliases=["duckdb-olap"],
        description="An in-process SQL OLAP database management system.",
        official_urls=["https://duckdb.org/"],
        default_packages=["duckdb"],
        known_repo_slugs=["duckdb/duckdb"],
        keywords=["duckdb", "in-process sql", "olap"],
    ),
    TechSpec(
        slug="redis",
        name="Redis",
        category="database",
        aliases=["redis-server"],
        description="In-memory data structure store used as a database, cache, and message broker.",
        official_urls=["https://redis.io/"],
        default_packages=["redis"],
        known_repo_slugs=["redis/redis"],
        keywords=["redis", "in-memory cache"],
    ),
    TechSpec(
        slug="postgresql",
        name="PostgreSQL",
        category="database",
        aliases=["postgres"],
        description="Powerful, open source object-relational database system.",
        official_urls=["https://www.postgresql.org/"],
        default_packages=["psycopg2", "asyncpg"],
        keywords=["postgresql", "postgres", "rdbms"],
    ),
    TechSpec(
        slug="sqlite",
        name="SQLite",
        category="database",
        aliases=["sqlite3"],
        description="Self-contained, serverless, zero-configuration, transactional SQL database engine.",
        official_urls=["https://www.sqlite.org/"],
        keywords=["sqlite", "sqlite3"],
    ),
    TechSpec(
        slug="uvicorn",
        name="Uvicorn",
        category="runtime",
        aliases=["uvicorn-server"],
        description="An ASGI web server implementation for Python.",
        official_urls=["https://www.uvicorn.org/"],
        default_packages=["uvicorn"],
        known_repo_slugs=["encode/uvicorn"],
        keywords=["uvicorn", "asgi server"],
    ),
]


class TechnologyRegistry:
    """Registry maintaining technology specifications and matching algorithms."""

    def __init__(self, initial_specs: list[TechSpec] | None = None):
        self._by_slug: dict[str, TechSpec] = {}
        self._alias_map: dict[str, TechSpec] = {}
        self._package_map: dict[str, TechSpec] = {}
        self._repo_map: dict[str, TechSpec] = {}
        self._keyword_map: dict[str, TechSpec] = {}

        specs = initial_specs if initial_specs is not None else DEFAULT_TAXONOMY
        for spec in specs:
            self.register(spec)

    def register(self, spec: TechSpec) -> None:
        """Register a new technology specification in all lookup indices."""
        technology_id(spec.slug)
        existing = self._by_slug.get(spec.slug)
        if existing is not None:
            if existing != spec:
                raise ValueError(f"Conflicting technology slug: {spec.slug}")
            return
        aliases = {token.strip().casefold() for token in (spec.name, spec.slug, *spec.aliases)}
        packages = {normalize_pypi_name(name) for name in spec.default_packages}
        repos = {canonical_github_url(name).removeprefix("https://github.com/")
                 for name in spec.known_repo_slugs}
        # Validate all indices before mutating any of them.
        for keys, index, label in (
            (aliases, self._alias_map, "alias"),
            (packages, self._package_map, "package"),
            (repos, self._repo_map, "repository"),
        ):
            for key in sorted(keys):
                if key in index and index[key].slug != spec.slug:
                    raise ValueError(f"Conflicting {label} mapping: {key}")
        self._by_slug[spec.slug] = spec
        self._alias_map.update(dict.fromkeys(aliases, spec))
        self._package_map.update(dict.fromkeys(packages, spec))
        self._repo_map.update(dict.fromkeys(repos, spec))
        for keyword in spec.keywords:
            self._keyword_map.setdefault(keyword.casefold(), spec)

    def get_by_slug(self, slug: str) -> TechSpec | None:
        """Retrieve by exact canonical slug."""
        return self._by_slug.get(slug)

    def lookup(self, term: str) -> TechSpec | None:
        """Lookup by name, slug, or alias (case-insensitive)."""
        clean = term.strip().casefold()
        if clean.startswith("tech:"):
            return self._by_slug.get(clean[5:]) if term.strip() == clean else None
        return self._alias_map.get(clean) or self._by_slug.get(clean) or self._keyword_map.get(clean)

    def lookup_by_package(self, package_name: str) -> TechSpec | None:
        """Lookup by PyPI package distribution name."""
        try:
            clean = normalize_pypi_name(package_name.strip())
        except ValueError:
            return None
        return self._package_map.get(clean)

    def lookup_by_repo(self, repo_full_name: str) -> TechSpec | None:
        """Match only a curated full owner/repository identity."""
        return self._repo_map.get(repo_full_name.strip().casefold())

    def lookup_by_topic(self, topic: str) -> TechSpec | None:
        """Lookup by GitHub topic tag."""
        clean = topic.strip().replace("_", "-").casefold()
        return self._alias_map.get(clean) or self._keyword_map.get(clean)

    def all_specs(self) -> list[TechSpec]:
        """Return all registered specifications."""
        return list(self._by_slug.values())
