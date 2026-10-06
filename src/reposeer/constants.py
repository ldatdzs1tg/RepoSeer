"""Constants, enumerations, and default values for RepoSeer."""

from enum import StrEnum
from pathlib import Path


class DataSource(StrEnum):
    """Supported data ingestion sources."""

    GITHUB = "github"
    PYPI = "pypi"
    DEPS_DEV = "deps_dev"
    OSV = "osv"
    HACKERNEWS = "hackernews"
    RSS = "rss"
    NEWS = "news"
    MANUAL = "manual"


class CollectionStatus(StrEnum):
    """Lifecycle status of a collection run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


class TechnologySource(StrEnum):
    """Source file or signal from which a technology was identified."""

    REQUIREMENTS_TXT = "requirements.txt"
    PYPROJECT_TOML = "pyproject.toml"
    PACKAGE_JSON = "package.json"
    CARGO_TOML = "Cargo.toml"
    POM_XML = "pom.xml"
    DOCKERFILE = "Dockerfile"
    HEURISTIC = "heuristic"
    GITHUB_LANGUAGE = "github_language"


class StorageFormat(StrEnum):
    """Storage format used across pipeline stages."""

    JSONL = "jsonl"
    PARQUET = "parquet"


# Project base directories
DEFAULT_CONFIG_PATH = Path("configs/default.yaml")
DEFAULT_SOURCES_PATH = Path("configs/sources.yaml")
DEFAULT_LOGGING_PATH = Path("configs/logging.yaml")
DEFAULT_DATA_DIR = Path("data")

# HTTP Defaults
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_FACTOR = 2.0
