"""Typed T-017 capture validation; the canonical native RawRecord stays unchanged."""

import base64
import binascii
import hashlib
import json
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from reposeer.contracts import RawRecord, validate_record
from reposeer.contracts.identity import normalize_article_text, text_sha256
from reposeer.contracts.models import (
    HttpUrl,
    JsonValue,
    NonEmpty,
    RawRecordId,
    Sha256,
    UtcTimestamp,
)
from reposeer.schemas.external.document import validate_publication_metadata


class ExternalDocumentPayload(BaseModel):
    """Capture fields are required even when nullable; native source keys stay nested."""

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    record_stage: Literal["external_document"]
    crawler_version: NonEmpty
    source: NonEmpty
    source_url: HttpUrl
    url: HttpUrl
    fetched_url: HttpUrl
    fetched_at: UtcTimestamp
    source_item_id: NonEmpty | None
    candidate_raw_record_id: RawRecordId | None
    discovery: dict[str, JsonValue] | None
    candidate_id: NonEmpty | None = None
    discovery_provider: NonEmpty | None = None
    discovery_source_item_id: NonEmpty | None = None
    discovery_source_url: HttpUrl | None = None
    discovery_references: list[dict[str, JsonValue]] = Field(default_factory=list)
    discovery_lineage_complete: bool = False
    response_body_base64: NonEmpty
    content_type: NonEmpty | None
    etag: NonEmpty | None
    last_modified: NonEmpty | None
    native_payload: JsonValue
    html: NonEmpty | None
    title: NonEmpty | None
    author: NonEmpty | None
    published_at: UtcTimestamp | None
    published_date: str | None = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    published_precision: Literal["timestamp", "date", "unknown"]
    updated_at: UtcTimestamp | None
    content: NonEmpty | None
    content_status: Literal["full", "snippet", "unavailable"]
    content_sha256: Sha256 | None
    word_count: int | None = Field(ge=0)
    content_available_at: UtcTimestamp | None
    historical_version_status: Literal["unverified", "observation_only"]
    relevance_status: Literal["unverified"]
    # Optional additions preserve replay of the first capture format.
    repository_scope: dict[str, JsonValue] | None = None
    availability_basis: Literal["observed_response"] | None = None

    @model_validator(mode="after")
    def coherent(self):
        validate_publication_metadata(
            self.published_at,
            self.published_date,
            self.published_precision,
            self.updated_at,
            self.fetched_at,
        )
        if self.content_status == "unavailable":
            if any(
                v is not None
                for v in (
                    self.content,
                    self.content_sha256,
                    self.word_count,
                    self.content_available_at,
                )
            ):
                raise ValueError("unavailable content needs null text/hash/count/availability")
        else:
            if self.content is None or self.content_sha256 is None or self.word_count is None:
                raise ValueError("extracted content requires text, hash and word count")
            if self.content != normalize_article_text(self.content):
                raise ValueError("content is not NFC/LF normalized")
            if self.content_sha256 != text_sha256(self.content):
                raise ValueError("content hash mismatch")
            if self.word_count != len(self.content.split()):
                raise ValueError("word count mismatch")
            if self.html is None and not isinstance(self.native_payload, dict):
                raise ValueError("extracted content requires retained HTML or native object")
        if self.content_available_at is not None and self.content_available_at > self.fetched_at:
            raise ValueError("content availability is after fetch")
        if self.availability_basis == "observed_response" and self.content_status != "unavailable":
            if (
                self.content_available_at != self.fetched_at
                or self.historical_version_status != "observation_only"
            ):
                raise ValueError("observation availability must equal fetch time")
        if self.candidate_raw_record_id is not None:
            if (
                not isinstance(self.discovery, dict)
                or self.discovery.get("raw_record_id") != self.candidate_raw_record_id
            ):
                raise ValueError("candidate lineage mismatch")
        if self.crawler_version == "0.3.0":
            if self.source == "web" and self.source_item_id is not None:
                raise ValueError("web captures cannot borrow discovery native IDs")
            if self.candidate_raw_record_id is not None and not any(
                ref.get("candidate_raw_record_id") == self.candidate_raw_record_id
                for ref in self.discovery_references
            ):
                raise ValueError("primary observation missing from discovery references")
        if self.discovery_lineage_complete and (
            not self.discovery_references
            or not all(ref.get("projection_verified") is True for ref in self.discovery_references)
        ):
            raise ValueError("complete discovery lineage requires validated projection references")
        return self


def validate_external_document_record(value: RawRecord) -> RawRecord:
    """Check envelope, extraction fields, raw bytes and cross-field provenance."""
    raw = validate_record(value)
    if (
        not isinstance(raw, RawRecord)
        or raw.source != "web_article"
        or raw.source_record_type != "article"
    ):
        raise ValueError("expected external document raw envelope")
    payload = ExternalDocumentPayload.model_validate(raw.payload)
    if (
        raw.source_url != payload.source_url
        or raw.retrieved_at != payload.fetched_at
        or raw.source_record_id != payload.source_item_id
        or raw.event_at != payload.published_at
    ):
        raise ValueError("capture and envelope provenance mismatch")
    if raw.http_status is None or not 200 <= raw.http_status < 300:
        raise ValueError("capture requires a successful HTTP response")
    if payload.availability_basis == "observed_response" and raw.available_at != raw.retrieved_at:
        raise ValueError("observation availability must equal retrieval time")
    try:
        body = base64.b64decode(payload.response_body_base64, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError("invalid response bytes") from error
    if not body.strip() or hashlib.sha256(body).hexdigest() != raw.raw_body_sha256:
        raise ValueError("raw response hash mismatch")
    if payload.html is not None:
        decoded = httpx.Response(
            200, content=body, headers={"content-type": payload.content_type or ""}
        ).text
        if payload.html != decoded:
            raise ValueError("retained HTML differs from response bytes")
    if payload.native_payload is not None:
        try:
            native = json.loads(body)
        except (ValueError, RecursionError) as error:
            raise ValueError("retained native payload is not response JSON") from error
        if native != payload.native_payload:
            raise ValueError("retained native payload differs from response JSON")
    return raw
