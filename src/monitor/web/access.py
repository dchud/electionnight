"""Checks on every dashboard request, before any route runs.

Bound to loopback, the dashboard accepts only the configured Host names, which
stops DNS rebinding. Bound to any other address it accepts any Host, because
the classroom address is not known in advance, and only this machine may use a
method other than GET. In both modes, a request other than a GET whose Origin
does not match its Host is refused.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


def is_loopback(host: str | None) -> bool:
    """Whether a bind or client address is this machine."""
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


def _host_name(header: str) -> str | None:
    try:
        return urlsplit(f"//{header}").hostname
    except ValueError:
        return None


def _origin_host(origin: str) -> str:
    try:
        return urlsplit(origin).netloc.lower()
    except ValueError:
        return ""


class AccessControl:
    """Pure ASGI, so a server-sent event stream passes through unbuffered."""

    def __init__(
        self, app: ASGIApp, allowed_hosts: list[str], lan: bool
    ) -> None:
        self.app = app
        self.allowed_hosts = {host.lower() for host in allowed_hosts}
        self.lan = lan

    def _refusal(self, scope: Scope) -> PlainTextResponse | None:
        headers = Headers(scope=scope)
        host = headers.get("host", "")
        if not self.lan and _host_name(host) not in self.allowed_hosts:
            return PlainTextResponse("Host not allowed", status_code=400)
        if scope["method"] == "GET":
            return None
        origin = headers.get("origin")
        if origin is not None and _origin_host(origin) != host.lower():
            return PlainTextResponse("Origin not allowed", status_code=403)
        client = scope.get("client")
        if self.lan and not (client and is_loopback(client[0])):
            return PlainTextResponse(
                "Only the host computer may change this", status_code=403
            )
        return None

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] == "http":
            refusal = self._refusal(scope)
            if refusal is not None:
                await refusal(scope, receive, send)
                return
        await self.app(scope, receive, send)
