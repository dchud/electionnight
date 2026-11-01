"""HTTP for everything a feed can point at: address-checked and bounded.

Feeds and the pages they link to are hostile by default. The client from
`build_client` refuses any host that resolves to a non-public address, on every
hop. `download` follows redirects itself, so a redirect body is never read, and
stops at a decoded byte cap and a total deadline.

An address refusal and a missed deadline are transport errors, because a Wi-Fi
login page or a failing network can cause either. Callers ask the network gate
before counting one against a site.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import zlib
from collections.abc import Awaitable, Callable

import httpx

from monitor.settings import Settings

HOST_CACHE_SECONDS = 30.0
HOST_CACHE_SIZE = 4096
MAX_HOPS = 10
ACCEPTED_ENCODINGS = "gzip, deflate"
BOUNDED_CODINGS = {"gzip", "x-gzip", "deflate"}
COMPRESSED_CODINGS = BOUNDED_CODINGS | {"br", "zstd", "compress"}

Resolve = Callable[[str], Awaitable[list[str]]]
CheckHop = Callable[[httpx.Request], Awaitable[None]]


class BlockedAddress(httpx.TransportError):
    """The host resolves to an address this process does not contact."""


class DeadlineExceeded(httpx.TimeoutException):
    """The whole download, across redirects, took too long."""


class BodyTooLarge(httpx.RequestError):
    """The decoded body passed its byte cap."""

    def __init__(
        self, message: str, *, status_code: int, request: httpx.Request
    ) -> None:
        super().__init__(message, request=request)
        self.status_code = status_code


async def resolve_host(host: str) -> list[str]:
    """Addresses for a name, looked up without blocking the event loop."""
    loop = asyncio.get_running_loop()
    found = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in found]


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class AddressGuard:
    """Refuses requests to hosts with any non-public address.

    Only an allowed host is remembered. A Wi-Fi login page can answer lookups
    with a private address, and a refusal must not outlast the login.
    """

    def __init__(
        self, allow_loopback: bool = False, resolve: Resolve | None = None
    ) -> None:
        self.allow_loopback = allow_loopback
        self.resolve = resolve or resolve_host
        self.known: dict[str, float] = {}

    def _permitted(self, text: str) -> bool:
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        address = mapped or address
        if address.is_loopback:
            return self.allow_loopback
        return address.is_global and not address.is_multicast

    async def _allowed(self, host: str, request: httpx.Request) -> bool:
        now = time.monotonic()
        if now < self.known.get(host, 0.0):
            return True
        try:
            addresses = (
                [host] if _is_address(host) else await self.resolve(host)
            )
        except OSError as error:
            raise httpx.ConnectError(
                f"cannot resolve {host}", request=request
            ) from error
        if not addresses or not all(map(self._permitted, addresses)):
            return False
        if len(self.known) >= HOST_CACHE_SIZE:
            self.known.clear()
        self.known[host] = now + HOST_CACHE_SECONDS
        return True

    async def check(self, request: httpx.Request) -> None:
        """An httpx request hook, which runs once per hop."""
        host = request.url.host
        if not await self._allowed(host, request):
            raise BlockedAddress(
                f"{host} has a non-public address", request=request
            )


def build_client(
    settings: Settings,
    timeout_seconds: float,
    max_connections: int,
    transport: httpx.AsyncBaseTransport | None = None,
    resolve: Resolve | None = None,
) -> httpx.AsyncClient:
    """A client that identifies the project and checks every address."""
    guard = AddressGuard(settings.poll.allow_loopback, resolve)
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        headers={"User-Agent": settings.poll.user_agent},
        limits=httpx.Limits(max_connections=max_connections),
        event_hooks={"request": [guard.check]},
        transport=transport,
    )


class _Inflater:
    """Decompresses a gzip or deflate stream up to a limit per call."""

    def __init__(self, coding: str) -> None:
        self.raw_allowed = coding == "deflate"
        window = zlib.MAX_WBITS if coding == "deflate" else zlib.MAX_WBITS | 16
        self.engine = zlib.decompressobj(window)

    def feed(self, chunk: bytes, limit: int) -> bytes:
        try:
            data = self.engine.decompress(chunk, limit)
        except zlib.error:
            # Some servers send deflate without its zlib header.
            if not self.raw_allowed:
                raise
            self.engine = zlib.decompressobj(-zlib.MAX_WBITS)
            data = self.engine.decompress(chunk, limit)
        self.raw_allowed = False
        return data


def _inflater_for(response: httpx.Response) -> _Inflater | None:
    """How to decode a body. Only one bounded coding can be kept in check.

    Any other value in the header, such as a charset sent by mistake, leaves
    the body as it arrived.
    """
    header = response.headers.get("content-encoding", "")
    named = [part.strip().lower() for part in header.split(",")]
    codings = [part for part in named if part in COMPRESSED_CODINGS]
    if not codings:
        return None
    if len(codings) > 1 or codings[0] not in BOUNDED_CODINGS:
        raise httpx.DecodingError(
            f"cannot bound Content-Encoding: {header}",
            request=response.request,
        )
    return _Inflater("deflate" if codings[0] == "deflate" else "gzip")


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    def too_large() -> BodyTooLarge:
        return BodyTooLarge(
            f"body over {max_bytes} bytes",
            status_code=response.status_code,
            request=response.request,
        )

    # A response built with its body in memory has nothing left to stream.
    if response.is_stream_consumed:
        body = await response.aread()
        if len(body) > max_bytes:
            raise too_large()
        return body
    inflater = _inflater_for(response)
    received = bytearray()
    async for chunk in response.aiter_raw():
        room = max_bytes - len(received)
        try:
            data = inflater.feed(chunk, room + 1) if inflater else chunk
        except zlib.error as error:
            raise httpx.DecodingError(
                str(error), request=response.request
            ) from error
        received += data
        if len(received) > max_bytes:
            raise too_large()
    return bytes(received)


def _with_body(response: httpx.Response, body: bytes) -> httpx.Response:
    """The response with its decoded body, closed and ready to read."""
    headers = [
        (name, value)
        for name, value in response.headers.multi_items()
        if name.lower() != "content-encoding"
    ]
    return httpx.Response(
        response.status_code,
        headers=headers,
        content=body,
        request=response.request,
    )


async def download(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int,
    deadline_seconds: float,
    headers: dict[str, str] | None = None,
    check_hop: CheckHop | None = None,
) -> httpx.Response:
    """GET a URL, following redirects, within a byte cap and a deadline.

    `check_hop` runs before each redirect is followed, and may raise to stop.
    """
    request = client.build_request(
        "GET",
        url,
        headers={"Accept-Encoding": ACCEPTED_ENCODINGS, **(headers or {})},
    )
    try:
        async with asyncio.timeout(deadline_seconds):
            for hop in range(MAX_HOPS + 1):
                if hop and check_hop is not None:
                    await check_hop(request)
                response = await client.send(
                    request, stream=True, follow_redirects=False
                )
                try:
                    following = response.next_request
                    if following is None:
                        body = await _read_capped(response, max_bytes)
                        return _with_body(response, body)
                finally:
                    await response.aclose()
                request = following
    except TimeoutError as error:
        raise DeadlineExceeded(
            f"no complete response in {deadline_seconds} s", request=request
        ) from error
    raise httpx.TooManyRedirects(
        f"more than {MAX_HOPS} redirects", request=request
    )
