"""Canonical ID, URL, text and timestamp edge cases."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from reposeer.contracts.identity import (
    add_calendar_months,
    article_id,
    canonical_article_url,
    canonical_github_url,
    checked_timestamp,
    format_utc_timestamp,
    migrate_legacy_id,
    normalize_article_text,
    normalize_pypi_name,
    package_id,
    parse_repo_id,
    repo_id,
    technology_id,
    text_sha256,
    validate_http_url,
)


def test_stable_ids_and_explicit_migration():
    assert repo_id("900000001") == "github:repository:900000001"
    assert parse_repo_id(repo_id("900000001")) == "900000001"
    assert package_id("My_Pkg.Name") == "pypi:my-pkg-name"
    assert normalize_pypi_name("My___Pkg...Name") == "my-pkg-name"
    assert technology_id("python") == "tech:python"
    assert canonical_github_url("Owner/Repo") == "https://github.com/owner/repo"
    assert migrate_legacy_id("gh_900000001") == repo_id("900000001")
    assert migrate_legacy_id("pypi_My_Pkg") == "pypi:my-pkg"
    with pytest.raises(ValueError):
        migrate_legacy_id("owner/repo")


@pytest.mark.parametrize(
    "value", [0, 1, 1.0, True, "0", "-1", "+1", "01", "1.0", "1e3", "", " 1", "??"]
)
def test_bad_repo_ids(value):
    with pytest.raises(ValueError):
        repo_id(value)


@pytest.mark.parametrize("value", ["", "_pkg", "pkg_", "pkg/name", "pkg name", "pkg!", "\u00e9"])
def test_bad_package_names(value):
    with pytest.raises(ValueError):
        package_id(value)


@pytest.mark.parametrize(
    "value",
    ["owner/repo.git", "https://github.com/a/b", "a/b/", "a/b?x=1", "a/b#x", "a/..", "a/b/c"],
)
def test_bad_github_names(value):
    with pytest.raises(ValueError):
        canonical_github_url(value)


def test_url_canonicalization_preserves_meaning():
    original = "HTTP://B\u00dcCHER.Example:80/News/Story/?b=2&utm_source=x&a=1&fbclid=x&a=3#section"
    expected = "http://xn--bcher-kva.example/News/Story/?b=2&a=1&a=3"
    assert canonical_article_url(original) == expected
    assert article_id(original) == article_id(expected + "&gclid=x")
    assert article_id(expected) != article_id(expected.replace("http:", "https:"))
    assert article_id(expected) != article_id(expected.replace("/News/", "/news/"))
    assert canonical_article_url("https://example.test:443/") == "https://example.test/"
    assert (
        canonical_article_url("https://example.test:8443/a?k=&k=2")
        == "https://example.test:8443/a?k=&k=2"
    )
    assert canonical_article_url("https://[::1]:443/a#x") == "https://[::1]/a"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test/a",
        "https:///x",
        "https://a:b@example.test/",
        "https://example.test/a b",
        "https://example.test/a\n",
        "https://bad_host/",
        "https://example.test:99999/",
        "https://example.test:0/",
        "https://example.test:/",
        "https://example.test/%xx",
        "https://example.test\\evil/",
        "https://-bad.test/",
    ],
)
def test_unsafe_or_malformed_urls(url):
    with pytest.raises(ValueError):
        validate_http_url(url)


def test_text_normalization_preserves_case_and_interior_spaces():
    normalized = "Caf\u00e9  Python\nLine"
    assert normalize_article_text("  Cafe\u0301  Python\r\nLine\r ") == normalized
    assert text_sha256(normalized) == text_sha256(" Cafe\u0301  Python\rLine ")
    assert text_sha256(normalized) != text_sha256(normalized.lower())


@pytest.mark.parametrize(
    "value",
    [
        "2024-02-30T00:00:00.000Z",
        "2024-01-01",
        "2024-01-01T00:00:00",
        "2024-01-01T00:00:00Z",
        "2024-01-01T00:00:00.00Z",
        "2024-01-01T00:00:00.0000Z",
        "2024-01-01T00:00:00.000+00:00",
        "2024-01-01T24:00:00.000Z",
        "2024-01-01T00:00:60.000Z",
    ],
)
def test_exact_utc_timestamp(value):
    with pytest.raises(ValueError):
        checked_timestamp(value)


def test_offset_conversion_truncation_and_calendar_horizon():
    offset = timezone(timedelta(hours=7))
    assert (
        format_utc_timestamp(datetime(2025, 1, 1, 0, 0, 0, 123999, offset))
        == "2024-12-31T17:00:00.123Z"
    )
    with pytest.raises(ValueError):
        format_utc_timestamp(datetime(2024, 1, 1))
    assert format_utc_timestamp(datetime(2024, 1, 1, tzinfo=UTC)) == "2024-01-01T00:00:00.000Z"
    assert add_calendar_months("2024-02-29T23:59:59.999Z", 12) == "2025-02-28T23:59:59.999Z"
    assert add_calendar_months("2023-01-31T00:00:00.000Z", 1) == "2023-02-28T00:00:00.000Z"
    assert add_calendar_months("2023-12-31T23:59:59.999Z", 3) == "2024-03-31T23:59:59.999Z"
