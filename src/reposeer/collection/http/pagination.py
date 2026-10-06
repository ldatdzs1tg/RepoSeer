"""Pagination utilities for handling chunked and cursor-based responses."""

from collections.abc import Callable, Iterator
from typing import Any

import httpx


def paginate_link_header(
    client: Any,
    initial_url: str,
    params: dict[str, Any] | None = None,
) -> Iterator[httpx.Response]:
    """Iterate through paginated endpoints using standard RFC 5988 'Link' headers."""
    next_url: str | None = initial_url
    current_params = params

    while next_url:
        response: httpx.Response = client.get(next_url, params=current_params)
        response.raise_for_status()
        yield response

        current_params = None  # Params are already encoded into Link next URL
        links = response.headers.get("link")
        if not links or 'rel="next"' not in links:
            break

        next_url = None
        for part in links.split(","):
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip("<> ")
                break


def paginate_page_number(
    fetch_page_fn: Callable[[int], list[Any]],
    start_page: int = 1,
    max_pages: int | None = None,
) -> Iterator[list[Any]]:
    """Iterate through pages using page-number query parameters."""
    current_page = start_page
    while True:
        if max_pages is not None and current_page >= start_page + max_pages:
            break
        items = fetch_page_fn(current_page)
        if not items:
            break
        yield items
        current_page += 1
