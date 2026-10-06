"""Application configuration using Pydantic Settings."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from reposeer.constants import DEFAULT_DATA_DIR


class StorageConfig(BaseModel):
    """Storage directory configurations."""

    base_dir: Path = DEFAULT_DATA_DIR
    raw_dir: Path = DEFAULT_DATA_DIR / "raw"
    processed_dir: Path = DEFAULT_DATA_DIR / "processed"
    features_dir: Path = DEFAULT_DATA_DIR / "features"
    reports_dir: Path = DEFAULT_DATA_DIR / "reports"
    metadata_dir: Path = DEFAULT_DATA_DIR / "metadata"


class CollectionConfig(BaseModel):
    """Collection parameters and constraints."""

    timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_factor: float = 2.0
    rate_limit_pause_seconds: int = 60
    max_workers: int = 4
    checkpoint_interval: int = 50


class Settings(BaseSettings):
    """Global configuration settings for RepoSeer."""

    model_config = SettingsConfigDict(
        env_prefix="REPOSEER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "RepoSeer"
    version: str = "0.1.0"
    env: str = "development"
    log_level: str = "INFO"

    # API keys and tokens
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    news_api_key: str | None = Field(default=None, alias="NEWS_API_KEY")

    # Sub-configurations
    storage: StorageConfig = Field(default_factory=StorageConfig)
    collection: CollectionConfig = Field(default_factory=CollectionConfig)

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> "Settings":
        """Load settings, optionally overriding defaults from a YAML file."""
        settings = cls()
        yaml_path = Path(config_path) if config_path else Path("configs/default.yaml")
        if yaml_path.exists():
            with open(yaml_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                if "storage" in data and isinstance(data["storage"], dict):
                    settings.storage = StorageConfig(**data["storage"])
                if "collection" in data and isinstance(data["collection"], dict):
                    settings.collection = CollectionConfig(**data["collection"])
                if "app" in data and isinstance(data["app"], dict):
                    if "name" in data["app"]:
                        settings.app_name = data["app"]["name"]
                    if "env" in data["app"]:
                        settings.env = data["app"]["env"]
        return settings


settings = Settings.load()
