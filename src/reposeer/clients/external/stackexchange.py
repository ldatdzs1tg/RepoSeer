"""Stack Exchange discovery provider querying Stack Exchange REST API v2.3."""

import html
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


class StackExchangeClient(DiscoveryProvider):
    """Discovery provider querying Stack Exchange advanced search API."""

    provider_name: str = "stackexchange"
    supports_historical_window: bool = True
    search_filters = {"sort": "creation", "order": "desc", "endpoint": "search/advanced"}

    def __init__(
        self,
        base_url: str = "https://api.stackexchange.com/2.3",
        *,
        site: str = "stackoverflow",
        api_key: str | None = None,
        http_client: HTTPClient | None = None,
        progress: CollectionProgress | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.site = site
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
        """Search a single paginated page of Stack Exchange questions."""
        if not isinstance(query, str) or not query.strip() or type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("query and bounded positive page_size are required")

        page = 1
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor.isdigit() or int(cursor) < 1:
                raise ValueError("cursor must be a positive integer string")
            page = int(cursor)

        endpoint = f"{self.base_url}/search/advanced"
        params: dict[str, Any] = {
            "site": self.site,
            "q": query,
            "page": page,
            "pagesize": min(page_size, 100),
            "sort": "creation",
            "order": "desc",
        }
        if window_start is not None:
            params["fromdate"] = int(window_start.timestamp())
        if window_end is not None:
            params["todate"] = int(window_end.timestamp())
        if self.api_key:
            params["key"] = self.api_key

        try:
            data = self.http.get_json(
                endpoint,
                params=params,
                headers={"Accept": "application/json"},
            )
            if not isinstance(data, dict):
                raise ValueError("invalid_stackexchange_payload")
            if data.get("error_id"):
                # Native error text may echo request credentials; retain only safe error categories.
                error_name = data.get("error_name")
                safe_name = error_name if error_name in {
                    "throttle_violation", "access_denied", "key_required", "invalid_access_token",
                    "temporarily_unavailable", "internal_error", "bad_parameter",
                } else "provider_error"
                raise HTTPClientError(f"StackExchange API error ({safe_name})")

            items = data.get("items")
            if not isinstance(items, list):
                raise ValueError("invalid_stackexchange_items")

            candidates = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("invalid_stackexchange_item")
                raw_title = item.get("title")
                if not isinstance(raw_title, str) or not raw_title.strip():
                    raise ValueError("invalid_stackexchange_title")
                title = html.unescape(raw_title)

                url = item.get("link")
                if not isinstance(url, str):
                    raise ValueError("invalid_stackexchange_url")
                validate_http_url(url)

                qid = item.get("question_id")
                source_item_id = f"se:{qid}" if qid is not None else None

                creation_date = item.get("creation_date")
                pub_date: datetime | None = None
                if isinstance(creation_date, (int, float)):
                    pub_date = datetime.fromtimestamp(creation_date, tz=UTC)

                score = item.get("score")
                if score is not None and (type(score) not in {int, float} or not isfinite(score)):
                    raise ValueError("invalid_stackexchange_score")

                ans_count = item.get("answer_count")
                comment_count = int(ans_count) if isinstance(ans_count, int) else None

                author: str | None = None
                owner = item.get("owner")
                if isinstance(owner, dict) and isinstance(owner.get("display_name"), str):
                    author = owner["display_name"]

                tags = item.get("tags")
                snippet = ", ".join(tags) if isinstance(tags, list) and tags else None

                candidates.append(
                    ExternalCandidate(
                        title=title,
                        url=url,
                        source="stackexchange",
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
                            "question_id", "title", "link", "creation_date", "tags", "score", "answer_count",
                        ) if key in item},
                    )
                )

            if type(data.get("has_more")) is not bool:
                raise ValueError("invalid_stackexchange_pagination")
            has_more = data["has_more"]
            next_cursor = str(page + 1) if has_more else None
            total_hits_reported = data.get("total") if isinstance(data.get("total"), int) else None

            pub_dates = [c.publication_date for c in candidates if c.publication_date is not None]
            observed_from = min(pub_dates) if pub_dates else None
            observed_to = max(pub_dates) if pub_dates else None

            result = DiscoveryPageResult(
                candidates=candidates,
                native_items_observed=len(items),
                page_number=page,
                has_more=has_more,
                next_cursor=next_cursor,
                total_hits_reported=total_hits_reported,
                observed_from=observed_from,
                observed_to=observed_to,
            )
        except (HTTPClientError, ValueError) as error:
            handle_collection_error(error, f"stackexchange:{query}", progress=self.progress)
            raise

        if self.progress is not None:
            self.progress.record_success("external.stackexchange.query", query, count=len(candidates))

        return result

    def search(self, query: str, limit: int = 50) -> list[ExternalCandidate]:
        """Search Stack Exchange questions matching query."""
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
