"""Bounded structured generation and explicit offline replay; SDK is live-only."""

import json
import math
import os
from time import perf_counter
from typing import Annotated, Literal, Protocol, get_args

from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_core import ErrorType
from pydantic_settings import BaseSettings, SettingsConfigDict

from reposeer.agents.prompts import PROMPT_VERSION as PROMPT_VERSION
from reposeer.agents.prompts import build_system_prompt
from reposeer.contracts.models import (
    ContractModel,
    Count,
    HttpUrl,
    NonEmpty,
    UtcTimestamp,
    UuidText,
    unique,
)
from reposeer.contracts.specialist_agents import (
    AgentRequest,
    AllowedUse,
    D06Evidence,
    EnrichmentSuggestion,
    ExtractedSignal,
    InformationGap,
    ResearchHandoffRequest,
    SignalType,
    Specialist,
)


class ProviderError(Exception):
    """A sanitized provider failure, never a raw API error response."""


PROVIDER_ERROR_CODES = frozenset({
    'provider_timeout', 'provider_connection_error', 'provider_auth_failed',
    'provider_permission_denied', 'provider_rate_limited', 'provider_model_or_endpoint_not_found',
    'provider_bad_request', 'provider_api_error', 'invalid_structured_output',
    'llm_refusal', 'llm_incomplete_output', 'llm_request_or_output_failed',
})


def _provider_error_code(error: Exception) -> str:
    """Classify machine-readable exception types/status only; never inspect error text/body."""
    if isinstance(error, ProviderError) and str(error) in PROVIDER_ERROR_CODES:
        return str(error)
    if isinstance(error, ValidationError | json.JSONDecodeError):
        return 'invalid_structured_output'
    try:
        import openai
    except ImportError:
        openai = None
    if openai is not None:
        for name, code in (
            ('APITimeoutError', 'provider_timeout'),
            ('APIConnectionError', 'provider_connection_error'),
            ('AuthenticationError', 'provider_auth_failed'),
            ('PermissionDeniedError', 'provider_permission_denied'),
            ('RateLimitError', 'provider_rate_limited'),
            ('NotFoundError', 'provider_model_or_endpoint_not_found'),
            ('BadRequestError', 'provider_bad_request'),
            ('UnprocessableEntityError', 'provider_bad_request'),
            ('LengthFinishReasonError', 'llm_incomplete_output'),
            ('ContentFilterFinishReasonError', 'llm_refusal'),
            ('APIResponseValidationError', 'invalid_structured_output'),
        ):
            exception_type = getattr(openai, name, None)
            if isinstance(exception_type, type) and isinstance(error, exception_type):
                return code
        status_type = getattr(openai, 'APIStatusError', None)
        if isinstance(status_type, type) and isinstance(error, status_type):
            return {400: 'provider_bad_request', 401: 'provider_auth_failed',
                    403: 'provider_permission_denied', 404: 'provider_model_or_endpoint_not_found',
                    422: 'provider_bad_request', 429: 'provider_rate_limited'}.get(
                        error.status_code, 'provider_api_error')
        api_type = getattr(openai, 'APIError', None)
        if isinstance(api_type, type) and isinstance(error, api_type):
            return 'provider_api_error'
    if isinstance(error, TimeoutError):
        return 'provider_timeout'
    if isinstance(error, ConnectionError):
        return 'provider_connection_error'
    return 'llm_request_or_output_failed'


class ProviderValidationIssue(ContractModel):
    field_path: NonEmpty
    error_type: NonEmpty


class ProviderCallDiagnostic(ContractModel):
    operation: Literal['research', 'extract', 'review']
    request_ref: NonEmpty
    duration_ms: Count
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    total_tokens: Count | None = None
    success: bool
    error_code: NonEmpty | None
    parse_failure: Literal[
        'invalid_json', 'schema_validation_failed', 'missing_parsed_output',
        'sdk_parse_failed', 'unknown_parse_failure',
    ] | None = None
    validation_errors: list[ProviderValidationIssue] = Field(default_factory=list)


def _parse_failure_details(error: Exception, schema) -> tuple[str, list[ProviderValidationIssue]]:
    """Expose bounded error types and schema-owned paths, never messages, inputs or bodies."""
    if isinstance(error, json.JSONDecodeError):
        return 'invalid_json', []
    if isinstance(error, ValidationError):
        errors = error.errors(include_input=False, include_context=False, include_url=False)
        document = schema.model_json_schema()
        known_fields = set(document.get('properties', {}))
        for definition in document.get('$defs', {}).values():
            known_fields.update(definition.get('properties', {}))
        issues = []
        for item in errors[:10]:
            parts = [str(part) if type(part) is int and 0 <= part <= 9007199254740991 else
                     part if isinstance(part, str) and part in known_fields else '<unknown_field>'
                     for part in item['loc'][:12]]
            issues.append(ProviderValidationIssue(
                field_path='.'.join(parts) or '$',
                error_type=item['type'] if item['type'] in get_args(ErrorType) else 'unknown_validation_error',
            ))
        return ('invalid_json' if any(item['type'] == 'json_invalid' for item in errors)
                else 'schema_validation_failed'), issues
    try:
        import openai
    except ImportError:
        openai = None
    sdk_error = getattr(openai, 'APIResponseValidationError', None)
    if isinstance(sdk_error, type) and isinstance(error, sdk_error):
        return 'sdk_parse_failed', []
    return 'unknown_parse_failure', []


def _usage_count(response, name: str) -> int | None:
    usage = getattr(response, 'usage', None)
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return value if type(value) is int and 0 <= value <= 9007199254740991 else None


class ResearchInsightDraft(ContractModel):
    source_ref: NonEmpty
    supporting_quote: Annotated[NonEmpty, Field(max_length=10000)]
    statement: Annotated[NonEmpty, Field(max_length=2000)]
    limitation: Annotated[NonEmpty, Field(max_length=2000)]


class ResearchDraft(ContractModel):
    insights: list[ResearchInsightDraft]
    information_gaps: list[InformationGap]
    enrichment_suggestions: list[EnrichmentSuggestion]


class CitationDraft(ContractModel):
    evidence_id: NonEmpty
    quote: NonEmpty


class SignalDraft(ContractModel):
    signal_type: SignalType
    statement: NonEmpty
    subject_id: NonEmpty
    event_at: UtcTimestamp | None
    announced_at: UtcTimestamp | None
    effective_at: UtcTimestamp | None
    citations: list[CitationDraft] = Field(min_length=1)
    proposed_use: AllowedUse
    uncertainty: NonEmpty | None


class ExtractionDraft(ContractModel):
    signals: list[SignalDraft]


class ReviewInput(ContractModel):
    cluster_id: NonEmpty
    signals: list[ExtractedSignal]
    passages: list[D06Evidence]


class ReviewDraft(ContractModel):
    selected_signal_id: NonEmpty | None
    reason: NonEmpty


class SpecialistLLM(Protocol):
    model_id: str

    def research(self, request: ResearchHandoffRequest) -> ResearchDraft: ...
    def extract(self, request: AgentRequest) -> ExtractionDraft: ...
    def review(self, conflict: ReviewInput) -> ReviewDraft: ...


class AgentLLMSettings(BaseSettings):
    """Live-only configuration. Environment overrides .env; keys stay redacted."""

    model_config = SettingsConfigDict(
        env_file='.env', env_file_encoding='utf-8', extra='ignore',
        populate_by_name=True, allow_inf_nan=False,
    )
    provider: Literal['openrouter', 'openai'] = Field(
        default='openrouter', validation_alias='REPOSEER_AGENT_PROVIDER')
    model: str = Field(default='', validation_alias='REPOSEER_AGENT_MODEL')
    openrouter_api_key: SecretStr = Field(default='', validation_alias='OPENROUTER_API_KEY')
    openai_api_key: SecretStr = Field(default='', validation_alias='OPENAI_API_KEY')
    openrouter_base_url: HttpUrl = Field(
        default='https://openrouter.ai/api/v1', validation_alias='OPENROUTER_BASE_URL')
    openrouter_http_referer: str = Field(default='', validation_alias='OPENROUTER_HTTP_REFERER')
    openrouter_app_title: str = Field(default='RepoSeer', validation_alias='OPENROUTER_APP_TITLE')
    timeout_seconds: float = Field(default=20, gt=0, le=120,
                                 validation_alias='REPOSEER_AGENT_TIMEOUT_SECONDS')
    max_output_tokens: int = Field(default=3200, ge=256, le=8000,
                                   validation_alias='REPOSEER_AGENT_MAX_OUTPUT_TOKENS')


class OpenAISpecialistLLM:
    def __init__(self, *, model: str | None = None, api_key: str | None = None,
                 client=None, timeout: float = 20, max_output_tokens: int = 3200,
                 base_url: str | None = None, default_headers: dict[str, str] | None = None):
        model = model or os.getenv('REPOSEER_AGENT_MODEL', '')
        if not model.strip():
            raise ValueError('missing_agent_model_configuration')
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError('timeout must be finite and between 0 and 120 seconds')
        if type(max_output_tokens) is not int or not 256 <= max_output_tokens <= 8000:
            raise ValueError('output token budget must be between 256 and 8000')
        self.model_id, self.max_output_tokens, self.timeout = model, max_output_tokens, timeout
        self._owns_client = client is None
        self._extra_body = None
        self.call_diagnostics: list[ProviderCallDiagnostic] = []
        if client is None:
            key = api_key or os.getenv('OPENAI_API_KEY', '')
            if not key.strip():
                raise ValueError('missing_openai_api_key')
            try:
                from openai import OpenAI
            except ImportError:
                raise ValueError('optional_functional_sdk_unavailable') from None
            try:
                client = OpenAI(api_key=key, timeout=timeout, max_retries=0,
                                **({'base_url': base_url} if base_url is not None else {}),
                                **({'default_headers': default_headers} if default_headers else {}))
            except Exception:
                raise ValueError('llm_client_initialization_failed') from None
        elif hasattr(client, 'with_options'):
            try:
                client = client.with_options(timeout=timeout, max_retries=0,
                    **({'base_url': base_url} if base_url is not None else {}),
                    **({'default_headers': default_headers} if default_headers else {}))
            except Exception:
                raise ValueError('llm_client_initialization_failed') from None
        self.client = client

    def _parse(self, data: ContractModel, schema, system_prompt: str):
        operation = {ResearchDraft: 'research', ExtractionDraft: 'extract', ReviewDraft: 'review'}[schema]
        request_ref = data.cluster_id if isinstance(data, ReviewInput) else data.request_id
        started = perf_counter()
        response, success, error_code = None, False, None
        parse_failure, validation_errors = None, []
        try:
            response = self.client.responses.parse(
                model=self.model_id, store=False, max_output_tokens=self.max_output_tokens,
                timeout=self.timeout,
                text_format=schema, input=[
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': json.dumps(data.model_dump(mode='json'))},
                ],
                **({'extra_body': self._extra_body} if self._extra_body is not None else {}),
            )
            if any(getattr(content, 'type', None) == 'refusal'
                   for output in getattr(response, 'output', [])
                   for content in getattr(output, 'content', [])):
                raise ProviderError('llm_refusal')
            if response.status != 'completed':
                raise ProviderError('llm_incomplete_output')
            value = getattr(response, 'output_parsed', None)
            if value is None:
                parse_failure = 'missing_parsed_output'
                raise ProviderError('invalid_structured_output')
            parsed = schema.model_validate(value.model_dump() if isinstance(value, schema) else value)
            success = True
            return parsed
        except Exception as error:
            error_code = _provider_error_code(error)
            if parse_failure is None and error_code in {'invalid_structured_output', 'llm_request_or_output_failed'}:
                parse_failure, validation_errors = _parse_failure_details(error, schema)
            raise ProviderError(error_code) from None
        finally:
            self.call_diagnostics.append(ProviderCallDiagnostic(
                operation=operation, request_ref=request_ref,
                duration_ms=max(0, int((perf_counter() - started) * 1000)),
                input_tokens=_usage_count(response, 'input_tokens'),
                output_tokens=_usage_count(response, 'output_tokens'),
                total_tokens=_usage_count(response, 'total_tokens'),
                success=success, error_code=error_code,
                parse_failure=parse_failure, validation_errors=validation_errors,
            ))

    def research(self, request: ResearchHandoffRequest) -> ResearchDraft:
        return self._parse(
            request, ResearchDraft,
            build_system_prompt(request.agent_kind, task="research"),
        )

    def extract(self, request: AgentRequest) -> ExtractionDraft:
        return self._parse(
            request, ExtractionDraft,
            build_system_prompt(request.agent_kind, task="extract"),
        )

    def review(self, conflict: ReviewInput) -> ReviewDraft:
        return self._parse(
            conflict, ReviewDraft,
            build_system_prompt(None, task="review"),
        )

    def close(self):
        if self._owns_client:
            self.client.close()


class OpenRouterSpecialistLLM(OpenAISpecialistLLM):
    """OpenRouter Responses transport; reuse all prompts, drafts and validators."""

    def __init__(self, *, model: str, api_key: str | None = None, client=None,
                 base_url: str = 'https://openrouter.ai/api/v1',
                 http_referer: str = '', app_title: str = 'RepoSeer',
                 timeout: float = 20, max_output_tokens: int = 3200):
        if not model.strip():
            raise ValueError('missing_agent_model_configuration')
        key = api_key or os.getenv('OPENROUTER_API_KEY', '')
        if client is None and not key.strip():
            raise ValueError('missing_openrouter_api_key')
        headers = {}
        if http_referer.strip():
            headers['HTTP-Referer'] = http_referer.strip()
        if app_title.strip():
            headers['X-OpenRouter-Title'] = app_title.strip()
        super().__init__(model=model, api_key=key, client=client, base_url=base_url,
                         default_headers=headers, timeout=timeout,
                         max_output_tokens=max_output_tokens)
        # Fail if routing cannot preserve the requested structured-output parameters.
        self._extra_body = {'provider': {'require_parameters': True}}


def create_specialist_llm(*, model: str | None = None, client=None,
                          settings: AgentLLMSettings | None = None) -> OpenAISpecialistLLM:
    """Construct live transport only on demand; never called by offline replay."""
    settings = settings if settings is not None else AgentLLMSettings()
    model_id = settings.model if model is None else model
    if not model_id.strip():
        raise ValueError('missing_agent_model_configuration')
    common = dict(model=model_id.strip(), client=client, timeout=settings.timeout_seconds,
                  max_output_tokens=settings.max_output_tokens)
    if settings.provider == 'openrouter':
        return OpenRouterSpecialistLLM(
            **common, api_key=settings.openrouter_api_key.get_secret_value(),
            base_url=str(settings.openrouter_base_url), http_referer=settings.openrouter_http_referer,
            app_title=settings.openrouter_app_title,
        )
    return OpenAISpecialistLLM(**common, api_key=settings.openai_api_key.get_secret_value())


class ResearchReplay(ContractModel):
    request_id: UuidText
    agent_kind: Specialist
    output: ResearchDraft


class ExtractionReplay(ContractModel):
    request_id: UuidText
    agent_kind: Specialist
    output: ExtractionDraft


class ReviewReplay(ContractModel):
    cluster_id: NonEmpty
    output: ReviewDraft


class ReplayBundle(ContractModel):
    schema_version: Literal['0.1.0']
    record_type: Literal['multi_agent_replay']
    provenance: Literal['synthetic', 'recorded']
    model_id: NonEmpty
    research: list[ResearchReplay]
    strict: list[ExtractionReplay]
    reviews: list[ReviewReplay]

    @model_validator(mode='after')
    def check(self):
        for lane in ('research', 'strict'):
            unique([(item.request_id, item.agent_kind) for item in getattr(self, lane)], lane + ' replay keys')
        unique([item.cluster_id for item in self.reviews], 'review replay keys')
        return self


class ReplaySpecialistLLM:
    """Replay stored structured provider content, without importing/creating an SDK client."""
    def __init__(self, bundle: ReplayBundle):
        self.bundle = ReplayBundle.model_validate(bundle.model_dump())
        self.model_id = self.bundle.model_id

    def _lookup(self, lane, request):
        entries = [item.output for item in getattr(self.bundle, lane)
                   if item.request_id == request.request_id and item.agent_kind == request.agent_kind]
        if not entries:
            raise ProviderError('replay_response_missing')
        return entries[0].model_copy(deep=True)

    def research(self, request: ResearchHandoffRequest) -> ResearchDraft:
        return self._lookup('research', request)

    def extract(self, request: AgentRequest) -> ExtractionDraft:
        return self._lookup('strict', request)

    def review(self, conflict: ReviewInput) -> ReviewDraft:
        entries = [item.output for item in self.bundle.reviews if item.cluster_id == conflict.cluster_id]
        if not entries:
            raise ProviderError('replay_review_missing')
        return entries[0].model_copy(deep=True)
