"""Metadata precision and network preflight independent of external services."""

from datetime import UTC, datetime

import pytest

from reposeer.collectors.external.crawler.extractor import ArticleExtractor
from reposeer.collectors.external.crawler.metadata import publication
from reposeer.collectors.external.crawler.safety import URLPolicy
from reposeer.exceptions import ExtractionError

NOW = datetime(2026, 10, 16, tzinfo=UTC)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:secret@example.com/a",
        "http://localhost/a",
        "http://app.local/a",
        "http://127.0.0.1/a",
        "http://10.0.0.1/a",
        "http://[::1]/a",
        "http://169.254.169.254/a",
        "https://example.com:8080/a",
        "http://224.0.0.1/a",
    ],
)
def test_non_public_urls_rejected(url):
    with pytest.raises(ValueError):
        URLPolicy(lambda host: ["93.184.216.34"]).validate(url)


@pytest.mark.parametrize(
    "addresses", [[], ["10.0.0.1"], ["93.184.216.34", "127.0.0.1"], ["::ffff:127.0.0.1"]]
)
def test_any_private_dns_answer_blocks_request(addresses):
    with pytest.raises(ValueError):
        URLPolicy(lambda host: addresses).validate("https://blog.example/page")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2024-03-01T09:30:00+07:00", ("2024-03-01T02:30:00.000Z", "2024-03-01", "timestamp")),
        ("2024-03-01", (None, "2024-03-01", "date")),
        ("2024-03-01T09:30:00", (None, "2024-03-01", "date")),
        ("2030-01-01T00:00:00Z", (None, None, "unknown")),
        ("2030-01-01", (None, None, "unknown")),
        ("not a date", (None, None, "unknown")),
        (None, (None, None, "unknown")),
    ],
)
def test_publication_precision(value, expected):
    assert publication(value, NOW) == expected


def test_extractor_date_only_and_parser_exception(monkeypatch):
    monkeypatch.setattr("trafilatura.extract", lambda *a, **kw: "  Cafe\u0301\r\nArticle body  ")
    monkeypatch.setattr("trafilatura.extract_metadata", lambda *a, **kw: None)
    doc = ArticleExtractor().extract(
        '<meta property="article:published_time" content="2024-03-01">',
        "https://blog.example/a",
        fetched_at=NOW,
    )
    assert doc.content == "Café\nArticle body"
    assert doc.published_at is None and doc.published_date == "2024-03-01"
    assert doc.published_precision == "date"

    def fail(*args, **kwargs):
        raise RuntimeError("parser internal data")

    monkeypatch.setattr("trafilatura.extract", fail)
    assert ArticleExtractor().extract("html", "https://blog.example") is None
    with pytest.raises(ExtractionError, match="parser_error"):
        ArticleExtractor().extract("html", "https://blog.example", strict=True)


def test_explicit_byline_recovers_author_and_date(monkeypatch):
    monkeypatch.setattr("trafilatura.extract", lambda *a, **kw: "Article body")
    monkeypatch.setattr("trafilatura.extract_metadata", lambda *a, **kw: None)
    doc = ArticleExtractor().extract(
        "<html><body><main><h1>Release</h1>Posted by Example Author on 2021-05-11<hr>Article body</main></body></html>",
        "https://blog.example/article",
        fetched_at=NOW,
    )
    assert doc.author == "Example Author"
    assert doc.published_date == "2021-05-11"
    assert doc.published_at is None


def test_json_ld_recovers_date_without_inventing_timezone(monkeypatch):
    monkeypatch.setattr("trafilatura.extract", lambda *a, **kw: "Article body")
    monkeypatch.setattr("trafilatura.extract_metadata", lambda *a, **kw: None)
    doc = ArticleExtractor().extract(
        '<html><head><script type="application/ld+json">{"@graph":[{"@type":"BlogPosting","datePublished":"2013-06-13T00:00:00","author":{"name":"Example Author"}}]}</script></head><body>Article body</body></html>',
        "https://blog.example/article",
        fetched_at=NOW,
    )
    assert doc.author == "Example Author"
    assert doc.published_date == "2013-06-13"
    assert doc.published_precision == "date" and doc.published_at is None


def test_script_text_is_not_a_visible_byline(monkeypatch):
    monkeypatch.setattr("trafilatura.extract", lambda *a, **kw: "Article body")
    monkeypatch.setattr("trafilatura.extract_metadata", lambda *a, **kw: None)
    doc = ArticleExtractor().extract(
        '<script>"Posted by Invented Author on 2021-05-11"</script><main>Article body</main>',
        "https://blog.example/article",
        fetched_at=NOW,
    )
    assert doc.author is None
    assert doc.published_date is None


def test_inferred_copyright_year_is_not_publication_date(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("trafilatura.extract", lambda *a, **kw: "Documentation content")
    monkeypatch.setattr(
        "trafilatura.extract_metadata",
        lambda *a, **kw: SimpleNamespace(
            title="Documentation",
            author=None,
            date="2010-01-01",
        ),
    )
    doc = ArticleExtractor().extract(
        "<html><main>Documentation content</main><footer>Copyright 2010–2026</footer></html>",
        "https://docs.example/tutorial/",
        fetched_at=NOW,
        strict=True,
    )
    assert doc.published_at is None
    assert doc.published_date is None
    assert doc.published_precision == "unknown"
