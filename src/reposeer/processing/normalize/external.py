"""T-017 capture to canonical T-011 Article, without entity or historical inference."""

import polars as pl

from reposeer.collectors.external.crawler.validation import (
    ExternalDocumentPayload,
    validate_external_document_record,
)
from reposeer.contracts import Article, NormalizationRequest, NormalizationResult, validate_record
from reposeer.contracts.identity import article_id, canonical_article_url
from reposeer.contracts.models import MissingField
from reposeer.processing.normalize.github import (
    normalization_error,
    normalization_result,
    observation_provenance,
)


class ExternalNormalizer:
    def normalize_urls(self, df: pl.DataFrame, url_col: str = "url") -> pl.DataFrame:
        """Preserve significant URL components using the canonical T-011 policy."""
        if url_col not in df.columns:
            return df
        return df.with_columns(pl.col(url_col).map_elements(
            canonical_article_url, return_dtype=pl.String).alias(url_col))


class ExternalDocumentNormalizer:
    def normalize(self, request: NormalizationRequest) -> NormalizationResult:
        request = validate_record(request)
        if not isinstance(request, NormalizationRequest):
            raise TypeError("expected NormalizationRequest")
        records, errors = [], []
        for raw in request.records:
            try:
                validate_external_document_record(raw)
                document = ExternalDocumentPayload.model_validate(raw.payload)
                if document.title is None:
                    errors.append(normalization_error(raw, "article_title_missing"))
                    continue
                # Native fetched_url is an API endpoint, not the item's document identity.
                canonical_url = canonical_article_url(
                    document.url if document.native_payload is not None else document.fetched_url)
                missing = [MissingField(field_path=name, reason="not_in_source", detail=None)
                           for name, value in {"authors": document.author, "language_code": None,
                                               "published_at": document.published_at,
                                               "content_text": document.content}.items() if value is None]
                records.append(validate_record(Article(
                    schema_version="0.1.0", record_type="article", article_id=article_id(canonical_url),
                    original_url=document.source_url, canonical_url=canonical_url, title=document.title,
                    article_kind="other", authors=[document.author] if document.author else None,
                    language_code=None, published_at=document.published_at,
                    published_date=document.published_date, published_precision=document.published_precision,
                    updated_at=document.updated_at, content_status=document.content_status,
                    content_text=document.content, content_sha256=document.content_sha256,
                    content_available_at=document.content_available_at, technology_mentions=[],
                    repo_ids=[], package_ids=[], provenance=observation_provenance(raw, missing),
                )))
            except (ValueError, TypeError, KeyError):
                errors.append(normalization_error(raw, "invalid_external_document"))
        return normalization_result(request, records, errors)
