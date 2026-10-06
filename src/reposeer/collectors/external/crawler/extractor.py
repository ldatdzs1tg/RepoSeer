"""Article extractor using Trafilatura according to T-017."""

import logging

import trafilatura

from reposeer.schemas.external.document import ArticleDocument

logger = logging.getLogger("reposeer.crawler.extractor")


class ArticleExtractor:
    """Extracts clean article text, title, and metadata from raw HTML."""

    def extract(self, html: str, url: str, source: str = "web") -> ArticleDocument | None:
        """Parse HTML using Trafilatura into ArticleDocument."""
        try:
            content = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=True,
                output_format="txt",
                url=url,
            )
            if not content:
                return None

            metadata = trafilatura.extract_metadata(html)
            title = metadata.title if metadata and metadata.title else None
            author = metadata.author if metadata and metadata.author else None
            word_count = len(content.split())

            return ArticleDocument(
                url=url,
                title=title,
                content=content,
                author=author,
                source=source,
                word_count=word_count,
            )
        except Exception as e:
            logger.warning("Extraction failed for '%s': %s", url, e)
            return None
