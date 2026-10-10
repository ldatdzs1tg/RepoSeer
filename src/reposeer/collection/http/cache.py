"""Opt-in, file-backed cache for successful GET responses."""

import base64
import hashlib
import json
import logging
import math
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from reposeer.collection.persistence import write_json_atomic

logger = logging.getLogger("reposeer.http.cache")


class ResponseCache:
    """Cache decoded response bytes with a TTL and request-specific keys.

    Keys include the URL, query, and all effective request headers, including
    authentication and Accept. Only the hash is persisted, never request headers.
    This is a basic TTL cache, not a full HTTP caching/revalidation implementation.
    """

    def __init__(
        self,
        directory: Path,
        ttl_seconds: float = 300.0,
        *,
        clock: Callable[[], float] | None = None,
    ):
        if ttl_seconds <= 0 or not math.isfinite(ttl_seconds):
            raise ValueError("Cache TTL must be finite and positive")
        self.directory = Path(directory)
        self.ttl_seconds = ttl_seconds
        self._clock = clock or time.time

    @staticmethod
    def _key(request: httpx.Request) -> str:
        identity = {
            "method": request.method,
            "url": str(request.url),
            "headers": sorted(request.headers.multi_items()),
        }
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _allows_caching(headers: httpx.Headers) -> bool:
        directives = {
            directive.partition("=")[0].strip().lower()
            for directive in headers.get("cache-control", "").split(",")
        }
        return not directives.intersection({"no-cache", "no-store"})

    def get(self, request: httpx.Request) -> httpx.Response | None:
        """Return a fresh response; expired, absent, or damaged entries are misses."""
        if request.method != "GET" or not self._allows_caching(request.headers):
            return None
        path = self.directory / f"{self._key(request)}.json"
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            expires_at = float(entry["expires_at"])
            if not math.isfinite(expires_at):
                raise ValueError("Invalid cache expiry")
            if expires_at <= self._clock():
                return None
            status_code = entry["status_code"]
            if (
                entry["version"] != 1
                or not isinstance(status_code, int)
                or not 200 <= status_code < 300
            ):
                raise ValueError("Invalid cached response")
            return httpx.Response(
                status_code,
                headers=entry["headers"],
                content=base64.b64decode(entry["content"], validate=True),
                request=request,
                extensions={"from_cache": True},
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            logger.warning("Ignoring unreadable HTTP cache entry %s", path.name)
            return None

    def store(self, request: httpx.Request, response: httpx.Response) -> None:
        """Persist successful GETs; cache failures never fail collection."""
        if (
            request.method != "GET"
            or not response.is_success
            or not self._allows_caching(request.headers)
            or not self._allows_caching(response.headers)
            or "set-cookie" in response.headers
            or "*" in {value.strip() for value in response.headers.get("vary", "").split(",")}
        ):
            return
        # HTTPX has already decompressed response.content. Do not decode it twice
        # when reconstructing the cached response.
        excluded_headers = {
            "content-encoding",
            "content-length",
            "transfer-encoding",
            "authorization",
            "proxy-authorization",
        }
        entry = {
            "version": 1,
            "expires_at": self._clock() + self.ttl_seconds,
            "status_code": response.status_code,
            "headers": [
                (key, value)
                for key, value in response.headers.multi_items()
                if key not in excluded_headers
            ],
            "content": base64.b64encode(response.content).decode("ascii"),
        }
        path = self.directory / f"{self._key(request)}.json"
        try:
            write_json_atomic(path, entry)
        except (OSError, ValueError):
            logger.warning("Could not persist HTTP cache entry %s", path.name)
