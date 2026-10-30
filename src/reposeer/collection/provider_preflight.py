"""One fail-fast setup gate for distributed V2; results contain no credential values."""

import re
from contextlib import ExitStack, nullcontext
from dataclasses import asdict, dataclass

from reposeer.clients.external.hackernews import HackerNewsClient
from reposeer.clients.external.news import NewsAPIProvider
from reposeer.clients.external.rss import RSSProvider, validate_feed_url
from reposeer.collection.http.client import HTTPClient
from reposeer.config import Settings
from reposeer.exceptions import HTTPClientError, RateLimitExceededError

EXTERNAL_SOURCES = ("hackernews", "devto", "stackexchange", "rss", "news")
CONFIGURATION = {"github": "GITHUB_TOKEN", "devto": "DEVTO_API_KEY",
                 "stackexchange": "STACKEXCHANGE_API_KEY", "news": "NEWS_API_KEY",
                 "rss": "REPOSEER_RSS_FEEDS"}


@dataclass(frozen=True)
class ProviderCheck:
    provider: str
    status: str
    configuration: str | None
    verification: str


def check_providers(*, live: bool = True, include_github: bool = True,
                    configuration: Settings | None = None,
                    http_client: HTTPClient | None = None) -> dict:
    """Validate all local values before any HTTP; injected transports support offline tests.

    External-only enrichment deliberately excludes even GitHub authentication calls.
    """
    configuration = configuration if configuration is not None else Settings.local()
    providers = (("github",) if include_github else ()) + EXTERNAL_SOURCES
    credentials = {"github": configuration.github_token, "devto": configuration.devto_api_key,
                   "stackexchange": configuration.stackexchange_api_key, "news": configuration.news_api_key}
    feeds = [value.strip() for value in configuration.rss_feeds.split(",")]
    checks = {}
    for provider in providers:
        status = "ready"
        if provider in credentials:
            value = credentials[provider]
            if not value or not value.strip() or re.search(
                r"your[_ -]|changeme|placeholder|example|replace[_ -]|<|\.\.\.", value, re.I):
                status = "missing_configuration"
        elif provider == "rss":
            try:
                if not all(feeds) or len(feeds) > 32 or len(feeds) != len(set(feeds)):
                    raise ValueError("invalid_feed_configuration")
                for feed in feeds:
                    validate_feed_url(feed)
            except ValueError:
                status = "invalid_feed" if configuration.rss_feeds.strip() else "missing_configuration"
        checks[provider] = ProviderCheck(provider, status, CONFIGURATION.get(provider), "static_only")
    if live and all(check.status == "ready" for check in checks.values()):
        with ExitStack() as stack:
            http = stack.enter_context(nullcontext(http_client) if http_client is not None else
                                       HTTPClient(timeout=15, max_retries=2, max_backoff_seconds=2))
            stack.callback(setattr, http, "cache", http.cache)
            http.cache = None  # Live verification must never succeed from a cached response.
            for provider in providers:
                status = "ready"
                try:
                    if provider == "github":
                        payload = http.get_json("https://api.github.com/user",
                                                headers={"Authorization": f"Bearer {credentials[provider]}"}, use_cache=False, follow_redirects=False)
                        if not isinstance(payload, dict) or type(payload.get("id")) is not int or not payload.get("login"):
                            status = "invalid_credentials"
                    elif provider == "devto":
                        payload = http.get_json("https://dev.to/api/users/me",
                                                headers={"api-key": credentials[provider]}, use_cache=False, follow_redirects=False)
                        if not isinstance(payload, dict) or type(payload.get("id")) is not int or not payload.get("username"):
                            status = "invalid_credentials"
                    elif provider == "stackexchange":
                        payload = http.get_json("https://api.stackexchange.com/2.3/info",
                                                params={"site": "stackoverflow"},
                                                headers={"Authorization": f"Bearer {credentials[provider]}"}, use_cache=False, follow_redirects=False)
                        if not isinstance(payload, dict):
                            status = "provider_unreachable"
                        elif payload.get("error_name") == "throttle_violation":
                            status = "quota_exhausted"
                        elif payload.get("error_name") in {"temporarily_unavailable", "internal_error"}:
                            status = "provider_unreachable"
                        elif payload.get("error_id") or type(payload.get("quota_max")) is not int or payload["quota_max"] <= 300:
                            # A public HTTP 200 with the anonymous quota does not verify the supplied key.
                            status = "invalid_credentials"
                        elif payload.get("quota_remaining") == 0 or payload.get("backoff"):
                            status = "quota_exhausted"
                        elif not isinstance(payload.get("items"), list) or not payload["items"]:
                            status = "provider_unreachable"
                    elif provider == "news":
                        NewsAPIProvider(credentials[provider], http_client=http).search_page("Python", page_size=1)
                    elif provider == "rss":
                        RSSProvider(feeds, http_client=http).load_feeds()
                    else:
                        HackerNewsClient(http_client=http).search_page("Python", page_size=1)
                except HTTPClientError as error:
                    status = getattr(error, "code", None) or (
                        "quota_exhausted" if isinstance(error, RateLimitExceededError) or error.status_code == 429 else
                        "invalid_credentials" if provider in {"github", "devto", "stackexchange"} and (
                            error.status_code in {401, 403} or provider == "stackexchange" and error.status_code == 400)
                        else "provider_unreachable")
                except (ValueError, TypeError) as error:
                    status = "invalid_feed" if provider == "rss" and str(error) != "dns_unavailable" else "provider_unreachable"
                checks[provider] = ProviderCheck(provider, status, CONFIGURATION.get(provider),
                                                 "live_verified" if status == "ready" else "not_verified")
    ready = all(check.status == "ready" for check in checks.values())
    return dict(status="ready" if ready else "provider_setup_incomplete", ready=ready,
                live=live, providers=[asdict(check) for check in checks.values()],
                setup="Configure the repository .env; run worker.py check-providers")
