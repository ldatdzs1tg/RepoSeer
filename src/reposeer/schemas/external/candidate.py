"""External discovery candidate contract."""

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field

from reposeer.schemas.common import BaseSchema, utc_now

SubjectMatchStatus = Literal[
    "exact_url_match",
    "high_confidence_metadata",
    "ambiguous_requires_crawl",
    "function_or_alternative_only",
    "rejected_noise",
]

ContextLevel = Literal["package_specific", "function_level", "alternative_level"]


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
    subject_match_status: SubjectMatchStatus = "ambiguous_requires_crawl"
    matched_queries: list[str] = Field(default_factory=list)


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
    """Evaluate subject match status for an external discovery candidate."""
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
        except Exception:
            url_is_valid = False

    if not title_clean and not url_is_valid:
        return "rejected_noise"

    cand_url_lower = url_clean.lower()
    cand_snippet_lower = snippet_clean.lower()
    cand_title_lower = title_clean.lower()

    # 2. Exact URL match
    target_url_substrings: list[str] = []

    if repo_full_name:
        fn_clean = repo_full_name.strip().lower()
        if "/" in fn_clean:
            target_url_substrings.append(f"github.com/{fn_clean}")

    if repo_url:
        ru = repo_url.strip().lower().rstrip("/")
        if ru.endswith(".git"):
            ru = ru[:-4]
        if "://" in ru:
            ru = ru.split("://", 1)[1]
        if ru:
            target_url_substrings.append(ru)

    if package_name:
        pn = package_name.strip().lower()
        target_url_substrings.append(f"pypi.org/project/{pn}")
        pn_norm = re.sub(r"[-_.]+", "-", pn)
        if pn_norm != pn:
            target_url_substrings.append(f"pypi.org/project/{pn_norm}")

    if known_urls:
        for ku in known_urls:
            ku_clean = ku.strip().lower().rstrip("/")
            if "://" in ku_clean:
                ku_clean = ku_clean.split("://", 1)[1]
            if ku_clean:
                target_url_substrings.append(ku_clean)

    for target in target_url_substrings:
        if target and (target in cand_url_lower or (cand_snippet_lower and target in cand_snippet_lower)):
            return "exact_url_match"

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

