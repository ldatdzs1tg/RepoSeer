"""Bounded RSS 2.0/Atom discovery over explicitly configured public HTTPS feeds."""

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlsplit
from xml.etree import ElementTree as ET

from reposeer.clients.external.base import DiscoveryPageResult, DiscoveryProvider, ProviderError
from reposeer.collection.http.client import HTTPClient
from reposeer.collectors.external.crawler.fetcher import HTMLFetcher
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.contracts.identity import canonical_article_url
from reposeer.schemas.external.candidate import ExternalCandidate


def validate_feed_url(url: str, *, policy: URLPolicy | None = None) -> None:
    if not isinstance(url, str) or urlsplit(url).scheme != "https":
        raise ValueError("feed_requires_public_https")
    if any(re.search(r"key|token|secret|password|signature|authorization", key, re.I)
           for key, _ in parse_qsl(urlsplit(url).query)):
        raise ValueError("feed_must_not_contain_credentials")
    # Static checking must not resolve DNS during --dry-run. Live fetches resolve again.
    (policy or URLPolicy(resolver=lambda host: ["8.8.8.8"])).validate(url)
    if urlsplit(url).hostname.lower().rstrip(".") in {"metadata.google.internal", "metadata"}:
        raise ValueError("non_public_host")


def parse_feed(body: bytes) -> list[dict]:
    # Accept UTF-8 feeds only, rejecting DTD/entities before parsing, including XML bombs.
    try:
        text = body.decode("utf-8-sig")
        if "\x00" in text or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
            raise ValueError("unsafe_feed_xml")
        root = ET.fromstring(text)
    except (UnicodeError, ET.ParseError) as error:
        raise ValueError("invalid_feed") from error
    def local(tag):
        return tag.rsplit("}", 1)[-1]
    if local(root.tag) not in {"rss", "feed"}:
        raise ValueError("invalid_feed")
    if local(root.tag) == "rss" and (root.attrib.get("version") != "2.0"
            or not any(local(child.tag) == "channel" for child in root)):
        raise ValueError("invalid_feed")
    result = []
    for item in root.iter():
        if local(item.tag) not in {"item", "entry"}:
            continue
        values = {}
        for child in item:
            tag = local(child.tag)
            value = "".join(child.itertext()).strip()
            if tag == "link":
                if child.attrib.get("rel", "alternate") == "alternate":
                    values["link"] = child.attrib.get("href") or value
            elif tag in {"title", "summary", "description", "content", "encoded", "published", "updated", "pubDate", "id", "guid", "author", "creator"}:
                values[tag] = value
        if not values.get("title") or not values.get("link"):
            continue
        values["link"] = canonical_article_url(values["link"])
        result.append(values)
    return result


class FeedURLPolicy:
    def __init__(self, policy: URLPolicy):
        self.policy = policy

    def validate(self, url: str) -> None:
        validate_feed_url(url, policy=self.policy)


class RSSProvider(DiscoveryProvider):
    provider_name = "rss"
    supports_historical_window = False
    search_filters = {"pagination": "observed_feed_entries_only", "coverage": "configured_feeds_only"}

    def __init__(self, feed_urls: list[str] | None = None, *,
                 http_client: HTTPClient | None = None, policy: URLPolicy | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.feed_urls = list(feed_urls or [])
        self.is_available = bool(self.feed_urls)
        self.unavailable_reason = "missing_configuration"
        if len(self.feed_urls) > 32 or len(self.feed_urls) != len(set(self.feed_urls)):
            raise ValueError("duplicate_feed_urls")
        for url in self.feed_urls:
            validate_feed_url(url)
        self._owns_http = http_client is None
        self.http = http_client if http_client is not None else HTTPClient()
        self.policy = FeedURLPolicy(policy or URLPolicy())
        self.clock = clock or (lambda: datetime.now(UTC))
        self._entries = None

    def execution_policy(self) -> dict:
        return {**super().execution_policy(), "feed_urls": self.feed_urls}

    def load_feeds(self) -> list[tuple[dict, str, datetime]]:
        if not self.feed_urls:
            raise ProviderError("missing_configuration")
        if self._entries is None:
            entries = []
            fetcher = HTMLFetcher(self.http)
            for url in self.feed_urls:
                validate_feed_url(url, policy=self.policy)
                response = fetcher.fetch_response(url, policy=self.policy, max_redirects=5,
                                                  max_response_bytes=2_000_000)
                # The generic fetcher checks public redirects; RSS also requires HTTPS throughout.
                if response.url.scheme != "https":
                    raise ValueError("feed_requires_public_https")
                observed = self.clock()
                entries.extend((entry, url, observed) for entry in parse_feed(response.content))
                if len(entries) > 10000:
                    raise ValueError("feed_entry_limit")
            self._entries = entries
        return self._entries

    def search_page(self, query: str, *, window_start: datetime | None = None,
                    window_end: datetime | None = None, cursor: str | None = None,
                    page_size: int = 50) -> DiscoveryPageResult:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("invalid_query")
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError("invalid_page_size")
        self.validate_cursor(cursor or "1")
        page = int(cursor or "1")
        terms = re.findall(r"[\w-]+", query.casefold())
        matches = []
        for entry, feed, observed in self.load_feeds():
            snippet = entry.get("summary") or entry.get("description") or entry.get("content") or entry.get("encoded") or ""
            text = (entry["title"] + " " + snippet).casefold()
            if not terms or not all(re.search(r"(?<![\w-])" + re.escape(term) + r"(?![\w-])", text) for term in terms):
                continue
            stamp = entry.get("published") or entry.get("pubDate") or entry.get("updated")
            date = None
            if stamp:
                try:
                    date = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    try:
                        date = parsedate_to_datetime(stamp)
                    except (ValueError, TypeError):
                        pass
                if date is not None and date.tzinfo is None:
                    date = None
            if date is not None and ((window_start and date < window_start) or (window_end and date > window_end)):
                continue
            stable_id = entry.get("id") or entry.get("guid") or entry["link"]
            matches.append(ExternalCandidate(
                title=entry["title"], url=entry["link"], source="rss", publication_date=date,
                query=query, discovered_at=observed, snippet=snippet[:2000] or None,
                author=entry.get("author") or entry.get("creator"), source_item_url=feed,
                source_item_id="rss:" + hashlib.sha256(stable_id.encode()).hexdigest(),
                native_payload={"feed_url": feed, "entry_id": stable_id, "summary_only": True}))
        matches = list({c.candidate_id: c for c in matches}.values())
        matches.sort(key=lambda c: c.candidate_id)
        more = page * page_size < len(matches)
        return DiscoveryPageResult(candidates=matches[(page - 1) * page_size:page * page_size],
                                   page_number=page, has_more=more, next_cursor=str(page + 1) if more else None,
                                   total_hits_reported=len(matches), native_items_observed=len(matches[(page - 1) * page_size:page * page_size]))

    def close(self) -> None:
        if self._owns_http:
            self.http.close()
