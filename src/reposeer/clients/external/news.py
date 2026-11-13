"""NewsAPI article discovery; current observations, with explicit plan failures."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime

from reposeer.clients.external.base import DiscoveryPageResult, DiscoveryProvider, ProviderError
from reposeer.collection.http.client import HTTPClient
from reposeer.contracts.identity import canonical_article_url
from reposeer.exceptions import HTTPClientError
from reposeer.schemas.external.candidate import ExternalCandidate


def news_error(payload: dict, status: int | None = None) -> ProviderError:
    code = payload.get("code")
    category = ("invalid_credentials" if code in {"apiKeyMissing", "apiKeyInvalid", "apiKeyDisabled"}
                or status == 401 else "quota_exhausted" if code in {"apiKeyExhausted", "rateLimited"}
                or status == 429 else "provider_plan_restricted" if code in {
                    "parameterInvalid", "maximumResultsReached", "sourceUnavailable", "upgradeRequired"}
                or status in {402, 426} else "provider_unreachable")
    return ProviderError(category, status_code=status)


class NewsAPIProvider(DiscoveryProvider):
    """Discover URLs; API snippets are never treated as complete article text.

    No plan is assumed to provide an exhaustive historical archive.
    """

    provider_name = "news"
    supports_historical_window = False
    search_filters = {"endpoint": "everything", "sortBy": "relevancy",
                      "coverage": "current_plan_accessible_articles_only"}

    def __init__(self, api_key: str | None = None, *,
                 http_client: HTTPClient | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.api_key = api_key
        self.is_available = bool(api_key and api_key.strip())
        self.unavailable_reason = "missing_configuration"
        self._blocked_error = None
        self.base_url = "https://newsapi.org/v2"
        self._owns_http = http_client is None
        self.http = http_client if http_client is not None else HTTPClient()
        self.clock = clock or (lambda: datetime.now(UTC))

    def search_page(self, query: str, *, window_start: datetime | None = None,
                    window_end: datetime | None = None, cursor: str | None = None,
                    page_size: int = 50) -> DiscoveryPageResult:
        if not isinstance(query, str) or not query.strip() or not 1 <= len(query) <= 500:
            raise ValueError("invalid_news_query")
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("invalid_page_size")
        if not self.api_key or not self.api_key.strip():
            raise ProviderError("missing_configuration")
        if self._blocked_error is not None:
            raise ProviderError(self._blocked_error[0], status_code=self._blocked_error[1])
        self.validate_cursor(cursor or "1")
        page, size = int(cursor or "1"), min(page_size, 100)
        params = {"q": query, "page": page, "pageSize": size, "sortBy": "relevancy"}
        if window_start is not None:
            params["from"] = window_start.isoformat()
        if window_end is not None:
            params["to"] = window_end.isoformat()
        try:
            payload = self.http.get_json(self.base_url + "/everything", params=params,
                                         headers={"X-Api-Key": self.api_key}, follow_redirects=False)
        except HTTPClientError as error:
            try:
                body = json.loads(error.response_body or "{}")
            except (ValueError, TypeError):
                body = {}
            failure = news_error(body if isinstance(body, dict) else {}, error.status_code)
            self._remember_failure(failure)
            raise failure from None
        if not isinstance(payload, dict):
            raise ValueError("invalid_news_payload")
        if payload.get("status") == "error":
            failure = news_error(payload)
            self._remember_failure(failure)
            raise failure
        articles, total = payload.get("articles"), payload.get("totalResults")
        if payload.get("status") != "ok" or not isinstance(articles, list) or type(total) is not int or total < 0:
            raise ValueError("invalid_news_payload")
        observed = self.clock()
        candidates = []
        for article in articles:
            if not isinstance(article, dict) or not isinstance(article.get("title"), str) or not article["title"].strip():
                raise ValueError("invalid_news_article")
            if not isinstance(article.get("url"), str):
                raise ValueError("invalid_news_url")
            url = canonical_article_url(article["url"])
            published = article.get("publishedAt")
            if published is not None and not isinstance(published, str):
                raise ValueError("invalid_news_publication_date")
            date = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
            if date is not None and date.tzinfo is None:
                raise ValueError("invalid_news_publication_date")
            candidates.append(ExternalCandidate(
                title=article["title"], url=url, source="news", query=query,
                publication_date=date, discovered_at=observed,
                snippet=article.get("description"), author=article.get("author"),
                source_item_id="news:" + hashlib.sha256(url.encode()).hexdigest(),
                source_item_url=url, native_payload={key: article[key] for key in
                    ("source", "author", "title", "description", "url", "publishedAt") if key in article}))
        if len(articles) < min(size, max(0, total - (page - 1) * size)):
            raise ProviderError("provider_plan_restricted")
        more = bool(articles) and page * size < total
        dates = [c.publication_date for c in candidates if c.publication_date is not None]
        return DiscoveryPageResult(candidates=candidates, page_number=page, has_more=more,
                                   next_cursor=str(page + 1) if more else None,
                                   total_hits_reported=total, native_items_observed=len(articles),
                                   observed_from=min(dates) if dates else None,
                                   observed_to=max(dates) if dates else None)

    def _remember_failure(self, failure: ProviderError) -> None:
        if failure.code in {"quota_exhausted", "invalid_credentials", "provider_plan_restricted"}:
            self._blocked_error = (failure.code, failure.status_code)

    def close(self) -> None:
        if self._owns_http:
            self.http.close()
