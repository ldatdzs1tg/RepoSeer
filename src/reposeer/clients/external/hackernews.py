"""Hacker News Algolia search client."""

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


class HackerNewsClient(DiscoveryProvider):
    """Discovery provider querying Hacker News Algolia search API."""

    provider_name: str = "hackernews"
    supports_historical_window: bool = True
    initial_cursor: str = "0"
    search_filters = {"tags": "story", "endpoint": "search_by_date_if_window_else_search"}

    def __init__(
        self,
        base_url: str = "https://hn.algolia.com/api/v1",
        *,
        http_client: HTTPClient | None = None,
        progress: CollectionProgress | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
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
        """Search a single paginated page of HN stories matching query."""
        if not isinstance(query, str) or not query.strip() or type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("query and bounded positive page_size are required")

        current_page = 0
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor.isdigit():
                raise ValueError("cursor must be a non-negative integer string")
            current_page = int(cursor)

        endpoint = (
            f"{self.base_url}/search_by_date"
            if (window_start is not None or window_end is not None)
            else f"{self.base_url}/search"
        )
        params: dict[str, Any] = {
            "query": query,
            "hitsPerPage": page_size,
            "tags": "story",
            "page": current_page,
        }

        numeric_filters: list[str] = []
        if window_start is not None:
            numeric_filters.append(f"created_at_i>={int(window_start.timestamp())}")
        if window_end is not None:
            numeric_filters.append(f"created_at_i<={int(window_end.timestamp())}")
        if numeric_filters:
            params["numericFilters"] = ",".join(numeric_filters)

        try:
            data = self.http.get_json(
                endpoint,
                params=params,
                headers={"Accept": "application/json"},
            )
            if not isinstance(data, dict) or not isinstance(data.get("hits"), list):
                raise ValueError("invalid_hackernews_search_payload")

            candidates = []
            for hit in data["hits"]:
                if not isinstance(hit, dict) or not isinstance(hit.get("title"), str) or not hit["title"].strip():
                    raise ValueError("invalid_hackernews_hit")
                url = hit.get("url")
                raw_id = hit.get("objectID")
                object_id = str(raw_id) if raw_id is not None else ""
                if url is None or (isinstance(url, str) and not url.strip()):
                    if not object_id or not object_id.isdecimal() or int(object_id) <= 0:
                        raise ValueError("invalid_hackernews_story_id")
                    url = f"https://news.ycombinator.com/item?id={object_id}"
                if not isinstance(url, str):
                    raise ValueError("invalid_hackernews_url")
                validate_http_url(url)

                score = hit.get("points")
                if score is not None and (type(score) not in {int, float} or not isfinite(score)):
                    raise ValueError("invalid_hackernews_score")

                pub_date: datetime | None = None
                created_at_i = hit.get("created_at_i")
                created_at = hit.get("created_at")
                if isinstance(created_at_i, (int, float)):
                    pub_date = datetime.fromtimestamp(created_at_i, tz=UTC)
                elif isinstance(created_at, str):
                    try:
                        pub_date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    except ValueError:
                        pub_date = None

                num_comments = hit.get("num_comments")
                comment_count = int(num_comments) if isinstance(num_comments, int) else None
                author = hit.get("author") if isinstance(hit.get("author"), str) else None
                story_text = hit.get("story_text") or hit.get("comment_text")
                snippet = str(story_text)[:500] if story_text else None
                source_item_id = f"hn:{object_id}" if object_id else None

                candidates.append(
                    ExternalCandidate(
                        title=hit["title"],
                        url=url,
                        source="hackernews",
                        source_item_id=source_item_id,
                        publication_date=pub_date,
                        query=query,
                        score=float(score) if score is not None else None,
                        discovered_at=self.clock(),
                        author=author,
                        comment_count=comment_count,
                        snippet=snippet,
                        source_item_url=(f"https://news.ycombinator.com/item?id={object_id}"
                                         if object_id.isdecimal() and int(object_id) > 0 else None),
                        native_payload={key: hit[key] for key in (
                            "objectID", "title", "url", "created_at_i", "created_at",
                            "author", "points", "num_comments",
                        ) if key in hit},
                    )
                )

            nb_pages = data.get("nbPages")
            if nb_pages is not None and (type(nb_pages) is not int or nb_pages < 0):
                raise ValueError("invalid_hackernews_pagination")
            has_more = ((current_page + 1) < nb_pages if nb_pages is not None
                        else len(data["hits"]) >= page_size)
            next_cursor = str(current_page + 1) if has_more else None
            total_hits_reported = data.get("nbHits") if isinstance(data.get("nbHits"), int) else None

            pub_dates = [c.publication_date for c in candidates if c.publication_date is not None]
            observed_from = min(pub_dates) if pub_dates else None
            observed_to = max(pub_dates) if pub_dates else None

            result = DiscoveryPageResult(
                candidates=candidates,
                native_items_observed=len(data["hits"]),
                page_number=current_page + 1,
                has_more=has_more,
                next_cursor=next_cursor,
                total_hits_reported=total_hits_reported,
                observed_from=observed_from,
                observed_to=observed_to,
            )
        except (HTTPClientError, ValueError) as error:
            handle_collection_error(error, f"hackernews:{query}", progress=self.progress)
            raise

        if self.progress is not None:
            self.progress.record_success("external.hackernews.query", query, count=len(candidates))

        return result

    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        """Search HN stories matching query."""
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
