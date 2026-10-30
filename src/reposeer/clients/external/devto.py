"""Dev.to / Forem discovery provider querying Dev.to REST API."""

from collections.abc import Callable
from datetime import UTC, datetime
from math import isfinite
from typing import Any, Self

from reposeer.clients.external.base import DiscoveryPageResult, DiscoveryProvider
from reposeer.collection.errors import handle_collection_error
from reposeer.collection.http.client import HTTPClient
from reposeer.collection.progress import CollectionProgress
from reposeer.contracts.identity import validate_http_url
from reposeer.exceptions import HTTPClientError
from reposeer.schemas.external.candidate import ExternalCandidate


class DevToClient(DiscoveryProvider):
    """Discovery provider querying Dev.to / Forem articles API."""

    provider_name: str = "devto"
    supports_historical_window: bool = False
    search_filters = {"endpoint": "articles/search", "ordering": "not_guaranteed_chronological"}

    def __init__(
        self,
        base_url: str = "https://dev.to/api",
        *,
        api_key: str | None = None,
        http_client: HTTPClient | None = None,
        progress: CollectionProgress | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._owns_http = http_client is None
        self.http = http_client if http_client is not None else HTTPClient(base_url=base_url)
        self.progress = progress
        self.clock = clock or (lambda: datetime.now(UTC))

    def search_page(
        self,
        query: str,
        *,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> DiscoveryPageResult:
        """Search a single paginated page of Dev.to articles with window cutoff filtering."""
        if not isinstance(query, str) or not query.strip() or type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("query and bounded positive page_size are required")

        page = 1
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor.isdigit() or int(cursor) < 1:
                raise ValueError("cursor must be a positive integer string")
            page = int(cursor)

        endpoint = f"{self.base_url}/articles/search"
        per_page = min(page_size, 100)
        params: dict[str, Any] = {
            "per_page": per_page,
            "page": page,
        }
        if query:
            params["q"] = query

        headers = {"Accept": "application/vnd.forem.api-v1+json"}
        if self.api_key:
            headers["api-key"] = self.api_key

        try:
            data = self.http.get_json(endpoint, params=params, headers=headers, follow_redirects=False)
            if not isinstance(data, list):
                raise ValueError("invalid_devto_payload")

            candidates = []

            for item in data:
                if not isinstance(item, dict):
                    raise ValueError("invalid_devto_item")

                raw_title = item.get("title")
                if not isinstance(raw_title, str) or not raw_title.strip():
                    raise ValueError("invalid_devto_title")

                url = item.get("url")
                if not isinstance(url, str):
                    raise ValueError("invalid_devto_url")
                validate_http_url(url)

                raw_id = item.get("id")
                source_item_id = f"devto:{raw_id}" if raw_id is not None else None

                raw_ts = item.get("published_timestamp") or item.get("published_at")
                pub_date: datetime | None = None
                if isinstance(raw_ts, str):
                    try:
                        pub_date = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    except ValueError:
                        pub_date = None

                # Search ranking is not chronological: filter items without terminating pagination.
                if pub_date is not None:
                    if window_start is not None and pub_date < window_start:
                        continue
                    if window_end is not None and pub_date > window_end:
                        continue

                score = item.get("positive_reactions_count")
                if score is not None and (type(score) not in {int, float} or not isfinite(score)):
                    raise ValueError("invalid_devto_score")

                comments_count = item.get("comments_count")
                comment_count = int(comments_count) if isinstance(comments_count, int) else None

                author: str | None = None
                user_info = item.get("user")
                if isinstance(user_info, dict):
                    author = user_info.get("name") or user_info.get("username")

                description = item.get("description")
                snippet = str(description)[:500] if description else None

                candidates.append(
                    ExternalCandidate(
                        title=raw_title,
                        url=url,
                        source="devto",
                        source_item_id=source_item_id,
                        publication_date=pub_date,
                        query=query,
                        score=float(score) if score is not None else None,
                        discovered_at=self.clock(),
                        author=author,
                        comment_count=comment_count,
                        snippet=snippet,
                        source_item_url=url,
                        native_payload={key: item[key] for key in (
                            "id", "title", "url", "published_timestamp", "published_at",
                            "description", "positive_reactions_count", "comments_count",
                        ) if key in item},
                    )
                )

            # Only an incomplete native page establishes the end of this search stream.
            if len(data) < per_page:
                has_more = False
            else:
                has_more = True

            next_cursor = str(page + 1) if has_more else None
            pub_dates = [c.publication_date for c in candidates if c.publication_date is not None]
            observed_from = min(pub_dates) if pub_dates else None
            observed_to = max(pub_dates) if pub_dates else None

            result = DiscoveryPageResult(
                candidates=candidates,
                native_items_observed=len(data),
                page_number=page,
                has_more=has_more,
                next_cursor=next_cursor,
                total_hits_reported=None,
                observed_from=observed_from,
                observed_to=observed_to,
            )
        except (HTTPClientError, ValueError) as error:
            handle_collection_error(error, f"devto:{query}", progress=self.progress)
            raise

        if self.progress is not None:
            self.progress.record_success("external.devto.query", query, count=len(candidates))

        return result

    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        """Search Dev.to articles matching query."""
        if not isinstance(query, str) or not query.strip() or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("query and bounded positive limit are required")
        return self.search_page(query, page_size=limit).candidates[:limit]

    def close(self) -> None:
        """Close the HTTP session if this provider created it."""
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
