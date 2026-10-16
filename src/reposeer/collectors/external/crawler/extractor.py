"""Article extractor using Trafilatura according to T-017."""

import logging
from datetime import datetime
from urllib.parse import urlsplit

import trafilatura

from reposeer.collectors.external.crawler.metadata import HTMLMetadata, publication
from reposeer.contracts.identity import normalize_article_text
from reposeer.exceptions import ExtractionError
from reposeer.schemas.common import utc_now
from reposeer.schemas.external.document import ArticleDocument

logger = logging.getLogger("reposeer.crawler.extractor")

# GitHub's SVG-only permalink anchors make headings look like link-only noise
# to Trafilatura. Remove those controls before extraction, keeping the heading
# text, original response bytes and metadata input intact.
GITHUB_HEADING_PERMALINKS = (
    "//article[contains(concat(' ', normalize-space(@class), ' '), ' markdown-body ')]"
    "//*[contains(concat(' ', normalize-space(@class), ' '), ' markdown-heading ')]"
    "/a[contains(concat(' ', normalize-space(@class), ' '), ' anchor ')"
    " and starts-with(@href, '#')]"
)


class ArticleExtractor:
    """Extracts clean article text, title, and metadata from raw HTML."""

    def extract(
        self,
        html: str,
        url: str,
        source: str = "web",
        *,
        fetched_at: datetime | None = None,
        strict: bool = False,
    ) -> ArticleDocument | None:
        """Parse HTML using Trafilatura into ArticleDocument."""
        try:
            content = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                output_format="txt",
                url=url,
                prune_xpath=(
                    GITHUB_HEADING_PERMALINKS
                    if urlsplit(url).hostname == "github.com"
                    else None
                ),
            )
            content = normalize_article_text(content or "")
            if not content:
                return None

            metadata = trafilatura.extract_metadata(html)
            title = metadata.title if metadata and metadata.title else None
            author = metadata.author if metadata and metadata.author else None
            word_count = len(content.split())
            observed = fetched_at or utc_now()
            explicit = HTMLMetadata()
            explicit.feed(html)
            byline_author, byline_date = explicit.byline()
            author = author or explicit.author or byline_author
            published_at, published_date, precision = publication(
                # Generic date extraction may infer a publication from a footer's
                # copyright year. Require an explicit publication field/byline.
                explicit.published or byline_date,
                observed,
            )
            updated_at, _, _ = publication(explicit.updated, observed)
            if updated_at and published_at and updated_at < published_at:
                updated_at = None

            return ArticleDocument(
                url=url,
                title=title,
                content=content,
                author=author,
                source=source,
                word_count=word_count,
                published_at=datetime.fromisoformat(published_at) if published_at else None,
                published_date=published_date,
                published_precision=precision,
                updated_at=datetime.fromisoformat(updated_at) if updated_at else None,
                extracted_at=observed,
            )
        except Exception as e:
            logger.warning("Article extraction failed (%s)", type(e).__name__)
            if strict:
                raise ExtractionError("parser_error") from e
            return None
