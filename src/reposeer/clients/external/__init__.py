"""External discovery client providers."""

from reposeer.clients.external.base import DiscoveryPageResult, DiscoveryProvider
from reposeer.clients.external.devto import DevToClient
from reposeer.clients.external.hackernews import HackerNewsClient
from reposeer.clients.external.news import NewsAPIProvider
from reposeer.clients.external.rss import RSSProvider
from reposeer.clients.external.stackexchange import StackExchangeClient

__all__ = [
    "DiscoveryPageResult",
    "DiscoveryProvider",
    "HackerNewsClient",
    "StackExchangeClient",
    "DevToClient",
    "RSSProvider",
    "NewsAPIProvider",
]
