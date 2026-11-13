"""Bounded deterministic T-018 relevance rules; no network, scores or model calls."""

import re
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reposeer.collectors.external.crawler.validation import validate_external_document_record
from reposeer.contracts import Article, RawRecord, validate_record
from reposeer.contracts.identity import (
    canonical_article_url,
    canonical_github_url,
    normalize_article_text,
    normalize_pypi_name,
    text_sha256,
)
from reposeer.contracts.models import HttpUrl, NonEmpty, PackageId, RawRecordId, RepoId, Sha256
from reposeer.technology.functional_profile import (
    FunctionalDraft,
    FunctionalProfileResult,
    ground_claims,
)

RULE_VERSION = "0.1.0-conservative"


class RelevanceModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class VerifiedTargetContext(RelevanceModel):
    """Trusted upstream assertions, with evidence references; this model does not verify URLs online."""
    package_id: PackageId
    repo_id: RepoId | None = None
    verified_repository_url: HttpUrl | None = None
    verified_package_url: HttpUrl | None = None
    verified_aliases: list[NonEmpty] = Field(default_factory=list, max_length=32)
    mapping_evidence_refs: list[NonEmpty] = Field(default_factory=list)
    optional_functional_profile: FunctionalProfileResult | None = None

    @classmethod
    def from_scope(cls, scope):
        """Use only the names attested by verified PyPI metadata and its canonical ID."""
        evidence = scope.resolve()
        name = scope.pypi.payload["info"]["name"]
        aliases = {name.casefold(): name, scope.pypi.package_id[5:].casefold(): scope.pypi.package_id[5:]}
        return cls(package_id=evidence["package_id"], repo_id=evidence["repo_id"],
                   verified_repository_url=evidence["repository_url"],
                   verified_package_url=f"https://pypi.org/project/{evidence['package_id'][5:]}/",
                   verified_aliases=list(aliases.values()),
                   mapping_evidence_refs=[entry["raw_record_id"] for entry in evidence["evidence"]])

    @model_validator(mode="after")
    def verified_identity(self):
        if self.repo_id is not None or self.verified_repository_url is not None:
            if self.repo_id is None or self.verified_repository_url is None or not self.mapping_evidence_refs:
                raise ValueError("repository_matching_requires_verified_identity_and_mapping")
            parsed = urlsplit(self.verified_repository_url)
            if canonical_github_url(parsed.path.strip("/")) != self.verified_repository_url:
                raise ValueError("repository_url_must_be_canonical_root")
        if self.verified_package_url is not None:
            parsed = urlsplit(self.verified_package_url)
            parts = parsed.path.strip("/").split("/")
            if (parsed.scheme != "https" or parsed.netloc != "pypi.org" or len(parts) != 2
                    or parts[0] != "project" or parsed.query or parsed.fragment
                    or "pypi:" + normalize_pypi_name(parts[1]) != self.package_id):
                raise ValueError("package_url_mismatch")
        if (self.verified_aliases or self.verified_package_url) and not self.mapping_evidence_refs:
            raise ValueError("verified_names_require_evidence")
        aliases = [alias.casefold() for alias in self.verified_aliases]
        if len(aliases) != len(set(aliases)) or any(len(alias) > 120 for alias in aliases):
            raise ValueError("aliases_must_be_unique_and_bounded")
        return self


class RelevanceSpan(RelevanceModel):
    text: NonEmpty
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)

    @model_validator(mode="after")
    def offsets(self):
        if self.end_offset - self.start_offset != len(self.text):
            raise ValueError("evidence_span_length_mismatch")
        return self


class LinkEvidence(RelevanceModel):
    url: HttpUrl
    anchor_text: str
    location: Literal["content", "reference"]


class RelevanceAssessment(RelevanceModel):
    article_id: str = Field(pattern=r"^article:sha256:[a-f0-9]{64}$")
    target_package_id: PackageId
    target_repo_id: RepoId | None
    content_sha256: Sha256 | None
    context_level: Literal["package_specific", "function_level", "alternative_level", "technology_level"] | None
    decision: Literal["relevant", "irrelevant", "unresolved"]
    match_method: NonEmpty
    reason_code: NonEmpty
    evidence_spans: list[RelevanceSpan]
    evidence_urls: list[HttpUrl]
    link_evidence: list[LinkEvidence]
    raw_record_ids: list[RawRecordId] = Field(min_length=1)
    rule_version: Literal["0.1.0-conservative"] = RULE_VERSION

    @model_validator(mode="after")
    def supported(self):
        if len(self.raw_record_ids) != len(set(self.raw_record_ids)):
            raise ValueError("duplicate_raw_record_ids")
        if self.decision == "relevant" and (not self.evidence_spans or self.context_level is None):
            raise ValueError("relevant_requires_substantive_content_evidence")
        if self.decision != "relevant" and self.context_level is not None:
            raise ValueError("nonpositive_decision_requires_null_context")
        return self


def validate_assessment_evidence(assessment: RelevanceAssessment, article: Article) -> None:
    """Revalidate structure and bind evidence to the exact canonical observation/revision."""
    article = validate_record(article)
    if not isinstance(article, Article):
        raise ValueError("assessment_requires_canonical_article")
    assessment = RelevanceAssessment.model_validate(assessment.model_dump(mode="python"))
    if assessment.article_id != article.article_id:
        raise ValueError("assessment_article_identity_mismatch")
    if assessment.content_sha256 != article.content_sha256:
        raise ValueError("assessment_content_revision_mismatch")
    if sorted(assessment.raw_record_ids) != sorted(article.provenance.raw_record_ids):
        raise ValueError("assessment_raw_reference_mismatch")
    content = article.content_text or ""
    for span in assessment.evidence_spans:
        if span.end_offset > len(content) or content[span.start_offset:span.end_offset] != span.text:
            raise ValueError("assessment_evidence_offset_mismatch")


def _reference_only_ranges(content: str) -> list[tuple[int, int]]:
    """Locate explicit reference sections/entries without rewriting canonical text."""
    ranges = []
    active = None
    level = None
    inline = False
    offset = 0
    for line in content.splitlines(keepends=True):
        text = line.strip()
        heading = re.fullmatch(r"(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?", text)
        heading_level = len(heading[1]) if heading else None
        label = heading[2] if heading else text
        reference = re.fullmatch(
            r"(references|bibliography|further reading|related links|sources|footer|navigation|menu)(?:\s*:\s*(.*))?",
            label, re.I,
        )
        plain_heading = re.fullmatch(r"[A-Z][A-Za-z \t-]{1,60}:", text)
        if active is not None and (
            (heading is not None and (level is None or heading_level <= level))
            or (plain_heading is not None and reference is None and level is None)
            or (inline and not text)
        ):
            ranges.append((active, offset))
            active = None
        if reference is not None and active is None:
            active, level, inline = offset, heading_level, bool(reference[2])
        offset += len(line)
    if active is not None:
        ranges.append((active, len(content)))
    return ranges


def _text_occurrences(content: str, text: str) -> list[tuple[int, int]]:
    """Align retained HTML prose through whitespace only; offsets remain original."""
    words = normalize_article_text(text).split()
    if not words:
        return []
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)"
    return [(match.start(), match.end()) for match in re.finditer(pattern, content)]


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.active = None
        self.links = []
        self.text_blocks = []
        self.text_parts = []
        self.text_excluded = False
        self.text_limited = False

    def _flush_text(self):
        if self.text_parts:
            if len(self.text_blocks) < 1000:
                self.text_blocks.append(("".join(self.text_parts), self.text_excluded))
            else:
                self.text_limited = True
            self.text_parts = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        scope = " ".join(values.get(key) or "" for key in ("class", "id", "role"))
        excluded = (tag in {"footer", "nav", "aside", "header", "head", "script", "style"}
                    or bool(re.search(r"\b(references|bibliography|related|sidebar|footer|navigation|menu)\b", scope, re.I))
                    or bool(self.stack and self.stack[-1][1]))
        if tag in {"p", "div", "section", "article", "footer", "nav", "aside", "header", "li", "br", "h1", "h2", "h3", "h4", "h5", "h6"} or excluded != self.text_excluded:
            self._flush_text()
        self.text_excluded = excluded
        # Void tags have no closing tag and must not affect scope tracking.
        if tag not in {"br", "hr", "img", "meta", "link", "input", "source", "area", "wbr", "embed", "param", "col", "base", "track"}:
            self.stack.append((tag, excluded))
        if tag == "a" and values.get("href") and len(self.links) < 500:
            self.active = [values["href"], [], excluded, self.text_parts]

    def handle_endtag(self, tag):
        if tag in {"p", "div", "section", "article", "footer", "nav", "aside", "header", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush_text()
        if tag == "a" and self.active is not None:
            self.links.append(self.active)
            self.active = None
        tags = [name for name, _ in self.stack]
        if tag in tags:
            del self.stack[len(tags) - 1 - tags[::-1].index(tag):]

    def handle_data(self, data):
        excluded = bool(self.stack and self.stack[-1][1])
        if excluded != self.text_excluded:
            self._flush_text()
        self.text_excluded = excluded
        self.text_parts.append(data)
        if self.active is not None:
            self.active[1].append(data)


def _profile_phrases(target: VerifiedTargetContext) -> list[str]:
    profile = target.optional_functional_profile
    if (profile is None or profile.status != "verified" or profile.package_id != target.package_id
            or profile.repo_id != target.repo_id or profile.scope_quality != "target_package"
            or not profile.source_revision_keys or not profile.evidence_refs):
        return []
    profile = FunctionalProfileResult.model_validate(profile.model_dump())
    draft = FunctionalDraft(**{name: getattr(profile, name) for name in FunctionalDraft.model_fields})
    grounded, _, diagnostics = ground_claims(draft, profile.evidence_refs)
    if diagnostics:
        return []
    # Official versioned sources only; repo-wide and foreign-package profiles cannot authorize a match.
    for ref in profile.evidence_refs:
        parts = urlsplit(ref.source_url)
        if parts.scheme != "https" or not ref.source_revision or text_sha256(ref.text) != ref.content_sha256:
            return []
        if parts.hostname == "pypi.org":
            prefix = "/pypi/" + target.package_id.removeprefix("pypi:") + "/"
            if parts.path != prefix + ref.source_revision + "/json":
                return []
        elif parts.hostname in {"github.com", "raw.githubusercontent.com"}:
            repo_path = urlsplit(target.verified_repository_url).path if target.verified_repository_url else ""
            suffix = ("/blob/" if parts.hostname == "github.com" else "/") + ref.source_revision + "/" + ref.source_path
            if not repo_path or parts.path[:len(repo_path)].casefold() != repo_path.casefold() or parts.path[len(repo_path):] != suffix:
                return []
        else:
            return []
    claims = [grounded["primary_function"], *grounded["use_cases"]]
    return sorted({claim.text for claim in claims if claim is not None and len(claim.text.split()) >= 3})


class ExternalRelevanceEvaluator:
    def evaluate(self, article: Article, target: VerifiedTargetContext,
                 *, raw: RawRecord | None = None) -> RelevanceAssessment:
        article = validate_record(article)
        if not isinstance(article, Article):
            raise TypeError("expected canonical Article")
        target = VerifiedTargetContext.model_validate(target.model_dump())
        if raw is not None:
            raw = validate_record(raw)
            if not isinstance(raw, RawRecord) or raw.raw_record_id not in article.provenance.raw_record_ids:
                raise ValueError("relevance_raw_provenance_mismatch")
            validate_external_document_record(raw)
            if (raw.retrieved_at != article.provenance.retrieved_at or raw.source_url != article.provenance.source_url
                    or raw.payload["content_sha256"] != article.content_sha256):
                raise ValueError("relevance_capture_content_mismatch")
        content = article.content_text or ""
        spans, links, urls = [], [], []
        html_limited = False
        decision, level, reason = "unresolved", None, "content_unavailable"
        sentences = list(re.finditer(r"[^.!?\n]+(?:[.!?]|$)", content[:200000]))[:1000]
        technical = re.compile(r"\b(python|pypi|pip install|package|library|framework|module)\b", re.I)
        reference_ranges = _reference_only_ranges(content[:200000])
        body_ranges, html_reference_ranges, uncertain_ranges = [], [], []
        parser = None
        if raw is not None and isinstance(raw.payload.get("html"), str):
            parser = _Links()
            parser.feed(raw.payload["html"][:2_000_000])
            parser.close()
            parser._flush_text()
            html_limited = len(raw.payload["html"]) > 2_000_000 or len(parser.links) == 500 or parser.text_limited
            blocks = {}
            for text, excluded in parser.text_blocks:
                normalized = " ".join(normalize_article_text(text).split())
                if normalized:
                    blocks.setdefault(normalized, []).append(excluded)
            for text, excluded_states in blocks.items():
                positions = _text_occurrences(content[:200000], text)
                if len(positions) == len(excluded_states):
                    for position, excluded in zip(positions, excluded_states, strict=True):
                        (html_reference_ranges if excluded else body_ranges).append(position)
                elif all(excluded_states):
                    html_reference_ranges.extend(positions)
                elif not any(excluded_states):
                    body_ranges.extend(positions)
                else:
                    uncertain_ranges.extend(positions)
        reference_link = reference_mention = reference_ambiguous = False

        def evidence_context(match):
            start = match.start() + len(match[0]) - len(match[0].lstrip())
            end = match.end() - len(match[0]) + len(match[0].rstrip())
            if any(start < right and end > left for left, right in reference_ranges):
                return "reference"
            body = any(left <= start and end <= right for left, right in body_ranges)
            if any(start < right and end > left for left, right in uncertain_ranges):
                if not body or any(left <= start and end <= right for left, right in uncertain_ranges):
                    return "ambiguous"
            if any(start < right and end > left for left, right in html_reference_ranges):
                covered = any(left <= start and end <= right for left, right in html_reference_ranges)
                if body and not covered:
                    return "body"  # Independent body prose may include the same short footer anchor.
                return "ambiguous" if body or not covered else "reference"
            if parser is not None and not body and (
                html_limited or any(excluded for _, excluded in parser.text_blocks)
            ):
                return "ambiguous"
            return "body"

        def substantive(match, *, link=False):
            nonlocal reference_link, reference_mention, reference_ambiguous
            context = evidence_context(match)
            if context == "ambiguous":
                reference_ambiguous = True
            elif context == "reference":
                if link:
                    reference_link = True
                else:
                    reference_mention = True
            return context == "body"

        comparison = re.compile(r"\b(migrat\w*|replac\w*|alternative|instead of|compared? to|versus|switch\w*)\b", re.I)
        aliases = [target.package_id, *target.verified_aliases]
        direct = []
        ambiguous = False
        canonical_verified = {canonical_article_url(url).rstrip("/") for url in (
            target.verified_package_url, target.verified_repository_url) if url}
        for match in re.finditer(r"https?://[^\s<>\[\]()]+", content[:200000]):
            try:
                url = canonical_article_url(match[0].rstrip(".,;"))
            except ValueError:
                continue
            if url.rstrip("/") in canonical_verified:
                start = max(content.rfind("\n", 0, match.start()) + 1, 0)
                end = content.find("\n", match.end())
                paragraph = content[start:end if end != -1 else len(content)]
                prose = re.sub(r"https?://\S+", "", paragraph)
                paragraph_match = re.compile(re.escape(paragraph)).search(content, start)
                if substantive(paragraph_match, link=True) and technical.search(prose) and len(prose.split()) >= 4:
                    direct.append(paragraph_match)
                    urls.append(url)
        for sentence in sentences:
            for alias in aliases:
                if re.search(r"(?<![\w-])" + re.escape(alias) + r"(?![\w-])", sentence[0], re.I):
                    if not substantive(sentence):
                        continue
                    name = re.escape(alias)
                    usage = re.search(r"\b(?:pip install|uv add|import|from)\s+" + name + r"\b", sentence[0], re.I)
                    named_tool = re.search(name + r"(?:\s+[\w-]+){0,4}\s+(?:package|library|framework|module)\b", sentence[0], re.I)
                    qualified = re.search(r"\b(?:Python|PyPI)\s+(?:package\s+|library\s+|framework\s+)?" + name + r"\b", sentence[0], re.I)
                    if alias == target.package_id or usage or named_tool or qualified:
                        direct.append(sentence)
                    else:
                        ambiguous = True
        if parser is not None:
            for href, pieces, excluded, block_parts in parser.links:
                try:
                    url = canonical_article_url(urljoin(article.canonical_url, href))
                except ValueError:
                    continue
                if url.rstrip("/") not in canonical_verified:
                    continue
                label = " ".join("".join(pieces).split())
                positions = _text_occurrences(content[:200000], "".join(block_parts))
                matching = next((sentence for sentence in sentences
                    if label and label in sentence[0] and technical.search(sentence[0])
                    and len(re.sub(r"https?://\S+", "", sentence[0]).split()) >= 4
                    and any(left <= sentence.start() + len(sentence[0]) - len(sentence[0].lstrip())
                            and sentence.end() <= right for left, right in positions)
                    and evidence_context(sentence) == "body"), None)
                location = "content" if matching is not None and not excluded else "reference"
                links.append(LinkEvidence(url=url, anchor_text=label, location=location))
                if excluded:
                    reference_link = True
                elif matching is not None:
                    direct.append(matching)
                    urls.append(url)
                else:
                    reference_ambiguous = True
        if article.content_status != "unavailable" and content:
            phrases = _profile_phrases(target)
            functional = [s for s in sentences if any(phrase.casefold() in s[0].casefold() for phrase in phrases)
                          and substantive(s)]
            if direct:
                decision, level, reason = "relevant", "package_specific", "verified_entity_in_substantive_content"
                spans = direct
                if any(comparison.search(s[0]) and any(phrase.casefold() in s[0].casefold() for phrase in phrases)
                       for s in direct):
                    level, reason = "alternative_level", "explicit_comparison_with_verified_function"
                    spans = [*direct, *functional]
            elif functional:
                decision, level, reason, spans = "relevant", "function_level", "verified_use_case_match", functional
            elif reference_ambiguous:
                reason = "reference_context_ambiguous"
            elif ambiguous:
                reason = "ambiguous_entity_mention"
            elif reference_link:
                decision, reason = "irrelevant", "reference_only_link"
            elif reference_mention:
                decision, reason = "irrelevant", "reference_only_mention"
            elif not phrases and any(re.search(r"\b(build|develop|create|process|parse|generate|serve)\b", s[0], re.I)
                                     for s in sentences if evidence_context(s) == "body"):
                reason = "functional_profile_unavailable"
            else:
                decision, reason = "irrelevant", "no_substantive_target_evidence"
        if decision != "relevant" and (len(content) > 200000 or len(sentences) == 1000 or html_limited):
            decision, level, reason = "unresolved", None, "analysis_limit_reached"
        unique = {(s.start(), s.end()): s for s in spans}
        assessment = RelevanceAssessment.model_validate(dict(
            article_id=article.article_id, target_package_id=target.package_id, target_repo_id=target.repo_id,
            content_sha256=article.content_sha256, context_level=level, decision=decision,
            match_method="deterministic_rules", reason_code=reason,
            evidence_spans=[RelevanceSpan(text=s[0], start_offset=s.start(), end_offset=s.end())
                            for _, s in sorted(unique.items())],
            evidence_urls=sorted(set(urls)), link_evidence=links,
            raw_record_ids=list(article.provenance.raw_record_ids),
        ))
        validate_assessment_evidence(assessment, article)
        return assessment
