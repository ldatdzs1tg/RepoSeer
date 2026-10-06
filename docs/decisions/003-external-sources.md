# ADR 003: External Sources Provider Abstraction

Discovery providers are abstracted through the `DiscoveryProvider` interface to decouple search endpoints (Hacker News, RSS, News APIs) from downstream scrapers and feature pipelines.
