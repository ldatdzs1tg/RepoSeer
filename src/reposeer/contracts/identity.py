"""Pure canonical identity, URL, timestamp and content helpers (contract 0.1.0)."""

import calendar
import hashlib
import ipaddress
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

FULL_NAME_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]+$"
UTC_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"


def repo_id(github_repo_id: str) -> str:
    if not isinstance(github_repo_id, str) or not re.fullmatch(r"[1-9][0-9]*", github_repo_id):
        raise ValueError("github_repo_id must be a positive decimal string")
    return "github:repository:" + github_repo_id


def parse_repo_id(value: str) -> str:
    prefix = "github:repository:"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError("invalid canonical repository ID")
    numeric = value[len(prefix) :]
    repo_id(numeric)
    return numeric


def normalize_pypi_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", name
    ):
        raise ValueError("invalid PyPI distribution name")
    return re.sub(r"[-_.]+", "-", name).lower()


def package_id(name: str) -> str:
    return "pypi:" + normalize_pypi_name(name)


def technology_id(slug: str) -> str:
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("invalid technology slug")
    return "tech:" + slug


def canonical_github_url(full_name: str) -> str:
    if (
        not isinstance(full_name, str)
        or not re.fullmatch(FULL_NAME_PATTERN, full_name)
        or full_name.lower().endswith(".git")
        or full_name.split("/")[1] in {".", ".."}
    ):
        raise ValueError("expected GitHub owner/repository, without clone suffix")
    return "https://github.com/" + full_name.lower()


def validate_http_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
        or "\\" in value
    ):
        raise ValueError("URL cannot contain whitespace or control characters")
    parts = urlsplit(value)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("URL requires http(s) and a hostname")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL credentials are prohibited")
    host = parts.hostname
    if ":" in host:
        ipaddress.IPv6Address(host)
    else:
        try:
            encoded = host.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise ValueError("invalid URL hostname") from error
        if len(encoded) > 253 or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in encoded.rstrip(".").split(".")
        ):
            raise ValueError("invalid URL hostname")
    if parts.port == 0 or parts.netloc.endswith(":"):
        raise ValueError("invalid URL port")
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise ValueError("invalid URL percent encoding")
    return value


def canonical_article_url(value: str) -> str:
    validate_http_url(value)
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    host = parts.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        host = "[" + host + "]"
    port = parts.port
    netloc = (
        host
        if port is None or (scheme, port) in {("http", 80), ("https", 443)}
        else f"{host}:{port}"
    )
    query = urlencode(
        [
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
        ]
    )
    return urlunsplit((scheme, netloc, parts.path, query, ""))


def article_id(url: str) -> str:
    return (
        "article:sha256:" + hashlib.sha256(canonical_article_url(url).encode("utf-8")).hexdigest()
    )


def normalize_article_text(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n")).strip()


def text_sha256(text: str) -> str:
    return hashlib.sha256(normalize_article_text(text).encode("utf-8")).hexdigest()


def checked_timestamp(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(UTC_PATTERN, value):
        raise ValueError("timestamp must be YYYY-MM-DDTHH:mm:ss.sssZ")
    datetime.fromisoformat(value)
    return value


def format_utc_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp requires an aware datetime")
    value = value.astimezone(UTC)
    return checked_timestamp(value.isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def add_calendar_months(value: str, months: int) -> str:
    checked_timestamp(value)
    if type(months) is not int:
        raise ValueError("months must be an integer")
    parsed = datetime.fromisoformat(value)
    month_index = parsed.year * 12 + parsed.month - 1 + months
    year, month = divmod(month_index, 12)
    month += 1
    day = min(parsed.day, calendar.monthrange(year, month)[1])
    return format_utc_timestamp(parsed.replace(year=year, month=month, day=day))


def migrate_legacy_id(value: str) -> str:
    """Explicit migration of an unambiguous legacy ID; never rewrites stored files."""
    if value.startswith("gh_"):
        return repo_id(value[3:])
    if value.startswith("pypi_"):
        return package_id(value[5:])
    raise ValueError("expected gh_<numeric ID> or pypi_<distribution name>")
