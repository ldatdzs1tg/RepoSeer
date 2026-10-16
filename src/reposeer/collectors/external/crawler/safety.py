"""Preflight checks for public web resources and every redirect destination."""

import ipaddress
import socket
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from reposeer.contracts.identity import validate_http_url


def resolve_public_host(host: str) -> list[str]:
    return list({item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)})


class URLPolicy:
    """Reject credentials, non-web ports, local names and non-public DNS answers.

    Inject a deterministic resolver for offline transports. This is a DNS preflight,
    not a network sandbox; deployments must use trusted DNS/egress controls.
    """

    def __init__(self, resolver: Callable[[str], Iterable[str]] = resolve_public_host):
        self.resolver = resolver

    def validate(self, url: str) -> None:
        validate_http_url(url)
        parts = urlsplit(url)
        host = parts.hostname.rstrip(".").lower()
        if parts.port not in {None, 80, 443}:
            raise ValueError("unsupported_port")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("non_public_host")
        try:
            addresses = [str(ipaddress.ip_address(host))]
        except ValueError:
            try:
                addresses = list(self.resolver(host))
            except OSError as error:
                raise ValueError("dns_unavailable") from error
        if not addresses or any(
            not ipaddress.ip_address(value).is_global or ipaddress.ip_address(value).is_multicast
            for value in addresses
        ):
            raise ValueError("non_public_address")
