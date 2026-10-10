"""Domain exceptions for RepoSeer."""


class RepoSeerError(Exception):
    """Base exception for all RepoSeer errors."""

    pass


class ConfigurationError(RepoSeerError):
    """Raised when configuration is missing, invalid, or cannot be loaded."""

    pass


class CollectionError(RepoSeerError):
    """Raised when a data collection task fails."""

    pass


class HTTPClientError(CollectionError):
    """Raised when an HTTP request fails after retries."""

    def __init__(
        self, message: str, status_code: int | None = None, response_body: str | None = None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class RateLimitExceededError(HTTPClientError):
    """Raised when an upstream API rate limit is reached."""

    def __init__(
        self,
        message: str,
        reset_timestamp: float | None = None,
        *,
        status_code: int = 429,
        response_body: str | None = None,
    ):
        super().__init__(message, status_code=status_code, response_body=response_body)
        self.reset_timestamp = reset_timestamp


class EntityNotFoundError(CollectionError):
    """Raised when a requested repository or entity does not exist upstream."""

    def __init__(self, entity_id: str):
        super().__init__(f"Entity '{entity_id}' not found")
        self.entity_id = entity_id


class ExtractionError(RepoSeerError):
    """Raised when content extraction (e.g. Trafilatura) fails."""

    pass


class StorageError(RepoSeerError):
    """Raised when persisting or querying data fails."""

    pass


class ValidationError(RepoSeerError):
    """Raised when data contracts or schema validation fail."""

    pass


class PipelineError(RepoSeerError):
    """Raised when pipeline orchestration encounters an unrecoverable error."""

    pass
