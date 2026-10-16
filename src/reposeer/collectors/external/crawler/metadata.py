"""Conservative publication precision: never invent a timezone or midnight."""

import json
import re
from datetime import UTC, date, datetime
from html.parser import HTMLParser

from reposeer.contracts.identity import format_utc_timestamp


def publication(value: object, fetched_at: datetime) -> tuple[str | None, str | None, str]:
    if not isinstance(value, str):
        return None, None, "unknown"
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            day = date.fromisoformat(value)
            if day <= fetched_at.astimezone(UTC).date():
                return None, value, "date"
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is not None and dt <= fetched_at:
                timestamp = format_utc_timestamp(dt)
                return timestamp, timestamp[:10], "timestamp"
            if dt.tzinfo is None and dt.date() <= fetched_at.astimezone(UTC).date():
                # Keep the source's calendar date, without inventing its UTC time.
                return None, dt.date().isoformat(), "date"
    except (ValueError, OverflowError):
        pass
    return None, None, "unknown"


class HTMLMetadata(HTMLParser):
    """Read explicit publication/edit fields before lossy date-only extraction."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.published: str | None = None
        self.updated: str | None = None
        self.author: str | None = None
        self._json_ld = False
        self._script_parts: list[str] = []
        self._text_parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "script":
            self._json_ld = (values.get("type") or "").lower() == "application/ld+json"
            self._script_parts = []
        if tag in {"script", "style"}:
            self._ignored += 1
        key = (values.get("property") or values.get("name") or values.get("itemprop") or "").lower()
        if tag == "meta":
            if key in {"article:published_time", "datepublished", "date", "pubdate"}:
                self.published = self.published or values.get("content")
            if key in {"article:modified_time", "datemodified"}:
                self.updated = self.updated or values.get("content")
            if key in {"author", "article:author"}:
                self.author = self.author or values.get("content")
        if tag == "time" and "datepublished" in key:
            self.published = self.published or values.get("datetime")

    def handle_data(self, data):
        if self._json_ld:
            self._script_parts.append(data)
        if not self._ignored:
            self._text_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._json_ld:
            try:
                self._read_json_ld(json.loads("".join(self._script_parts)))
            except (ValueError, RecursionError):
                pass
            self._json_ld = False
        if tag in {"script", "style"}:
            self._ignored = max(0, self._ignored - 1)

    def _read_json_ld(self, value):
        if isinstance(value, list):
            for item in value:
                self._read_json_ld(item)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if isinstance(types, list) and any(
                t in {"Article", "BlogPosting", "NewsArticle"} for t in types if isinstance(t, str)
            ):
                for field, attr in [("datePublished", "published"), ("dateModified", "updated")]:
                    if isinstance(value.get(field), str):
                        setattr(self, attr, getattr(self, attr) or value[field])
                author = value.get("author")
                if isinstance(author, dict):
                    author = author.get("name")
                if isinstance(author, str):
                    self.author = self.author or author
            if "@graph" in value:
                self._read_json_ld(value["@graph"])

    def byline(self) -> tuple[str | None, str | None]:
        """Read an explicit early-page byline; never guess from a domain name."""
        text = " ".join(" ".join(self._text_parts).split())[:2000]
        match = re.search(r"\bPosted by (.{1,100}?) on (\d{4}-\d{2}-\d{2})\b", text)
        return (match[1].strip(), match[2]) if match else (None, None)
