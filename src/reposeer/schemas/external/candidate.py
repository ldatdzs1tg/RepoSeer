"""External discovery candidate contract."""

import hashlib
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, computed_field

from reposeer.contracts.identity import (
    canonical_article_url,
    canonical_github_url,
    format_utc_timestamp,
    normalize_pypi_name,
)
from reposeer.schemas.common import BaseSchema, utc_now

SubjectMatchStatus = Literal[
    "exact_url_match",
    "high_confidence_metadata",
    "ambiguous_requires_crawl",
    "function_or_alternative_only",
    "technology_context_only",
    "rejected_noise",
]

ContextLevel = Literal["package_specific", "function_level", "alternative_level", "technology_level"]


class ExternalCandidate(BaseSchema):
    """External article, post, or mention identified during discovery."""

    title: str
    url: str
    source: str  # "hackernews", "rss", "newsapi", "stackexchange", "devto"
    publication_date: datetime | None = None
    query: str | None = None
    score: float | None = None
    discovered_at: datetime = Field(default_factory=utc_now)
    source_item_id: str | None = None
    snippet: str | None = None
    author: str | None = None
    comment_count: int | None = None
    query_id: str | None = None
    query_intent: str | None = None
    context_level: ContextLevel = "package_specific"
    tech_slug: str | None = None
    raw_record_ids: list[str] = Field(default_factory=list)
    subject_match_status: SubjectMatchStatus = "ambiguous_requires_crawl"
    matched_queries: list[str] = Field(default_factory=list)
    query_associations: list[dict[str, str]] = Field(default_factory=list)
    source_item_url: str | None = None
    native_payload: dict[str, Any] = Field(default_factory=dict)

    @computed_field
    @property
    def candidate_id(self) -> str:
        native_key = self.source_item_id or canonical_article_url(self.url)
        identity = f"{self.source.casefold()}:{native_key}"
        return "candidate:sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @computed_field
    @property
    def published_at(self) -> str | None:
        """T-008 serialization name; publication_date remains compatible with provider callers."""
        return format_utc_timestamp(self.publication_date) if self.publication_date is not None else None


def evaluate_subject_match(
    url: str | None = None,
    title: str | None = None,
    snippet: str | None = None,
    *,
    query_intent: str | None = None,
    context_level: ContextLevel = "package_specific",
    repo_full_name: str | None = None,
    repo_url: str | None = None,
    package_name: str | None = None,
    primary_keywords: list[str] | set[str] | None = None,
    known_urls: list[str] | set[str] | None = None,
) -> SubjectMatchStatus:
    """Discovery heuristics only: even an exact link does not verify article relevance."""
    if isinstance(url, ExternalCandidate):
        candidate = url
        url = candidate.url
        title = title if title is not None else candidate.title
        snippet = snippet if snippet is not None else candidate.snippet
        query_intent = query_intent if query_intent is not None else candidate.query_intent
        if context_level == "package_specific" and candidate.context_level != "package_specific":
            context_level = candidate.context_level

    url_clean = (url or "").strip()
    title_clean = (title or "").strip()
    snippet_clean = (snippet or "").strip()

    # 1. Rejected noise if both title and url are empty or malformed
    url_is_valid = False
    if url_clean:
        try:
            parsed = urlsplit(url_clean)
            if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
                url_is_valid = True
        except ValueError:
            url_is_valid = False

    if not title_clean and not url_is_valid:
        return "rejected_noise"

    cand_snippet_lower = snippet_clean.lower()
    cand_title_lower = title_clean.lower()

    # Parse complete URL tokens, including schemeless official links, with host/path boundaries.
    links = [url_clean] if url_is_valid else []
    links.extend(match.group().rstrip(".,;:!?)\"'>]") for match in re.finditer(
        r"(?<![\w./@-])(?:https?://[^\s<>\"']+|(?:github\.com|pypi\.org)/[^\s<>\"']+)",
        snippet_clean + " " + title_clean,
    ))
    target_repo = None
    try:
        if repo_full_name:
            target_repo = canonical_github_url(repo_full_name)
        elif repo_url:
            parsed_target = urlsplit(canonical_article_url(repo_url))
            if parsed_target.hostname == "github.com" and parsed_target.port is None:
                target_repo = canonical_github_url(parsed_target.path.strip("/").removesuffix(".git"))
    except ValueError:
        pass
    for link in links:
        try:
            parsed = urlsplit(canonical_article_url(link if "://" in link else "https://" + link))
            parts = parsed.path.strip("/").split("/")
            if parsed.hostname == "github.com" and parsed.port is None and len(parts) >= 2 and target_repo:
                if canonical_github_url("/".join(parts[:2]).removesuffix(".git")) == target_repo:
                    return "exact_url_match"
            if (parsed.hostname == "pypi.org" and parsed.port is None and len(parts) >= 2
                    and parts[0] == "project" and package_name
                    and normalize_pypi_name(parts[1]) == normalize_pypi_name(package_name)):
                return "exact_url_match"
            for known in known_urls or []:
                upstream = urlsplit(canonical_article_url(known))
                root = upstream.path.rstrip("/")
                if (parsed.scheme == upstream.scheme and parsed.netloc == upstream.netloc
                        and (parsed.path.rstrip("/") == root or parsed.path.startswith(root + "/"))
                        and (not upstream.query or parsed.query == upstream.query)):
                    return "exact_url_match"
        except ValueError:
            continue

    if context_level == "technology_level":
        return "technology_context_only"

    # 3. High confidence metadata
    if repo_full_name:
        fn_clean = repo_full_name.strip().lower()
        if fn_clean and (fn_clean in cand_title_lower or (cand_snippet_lower and fn_clean in cand_snippet_lower)):
            return "high_confidence_metadata"

    target_names: set[str] = set()
    if package_name:
        target_names.add(package_name.strip().lower())
    if repo_full_name and "/" in repo_full_name:
        target_names.add(repo_full_name.split("/")[1].strip().lower())

    if target_names and primary_keywords:
        has_name_in_title = any(
            bool(re.search(rf"\b{re.escape(name)}\b", cand_title_lower))
            for name in target_names
            if name
        )
        has_keyword_in_title = any(
            bool(re.search(rf"\b{re.escape(kw.strip().lower())}\b", cand_title_lower))
            for kw in primary_keywords
            if kw and kw.strip()
        )
        if has_name_in_title and has_keyword_in_title:
            return "high_confidence_metadata"

    # 4. Function or alternative only
    if context_level in ("function_level", "alternative_level"):
        directly_named = False
        if repo_full_name and (
            repo_full_name.strip().lower() in cand_title_lower
            or (cand_snippet_lower and repo_full_name.strip().lower() in cand_snippet_lower)
        ):
            directly_named = True
        elif target_names:
            for name in target_names:
                if name and (
                    re.search(rf"\b{re.escape(name)}\b", cand_title_lower)
                    or (cand_snippet_lower and re.search(rf"\b{re.escape(name)}\b", cand_snippet_lower))
                ):
                    directly_named = True
                    break
        if not directly_named:
            return "function_or_alternative_only"

    # 5. Otherwise: ambiguous, requires crawl
    return "ambiguous_requires_crawl"

