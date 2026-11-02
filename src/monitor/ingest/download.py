"""HTTP for everything a feed can point at: address-checked and bounded.

Feeds and the pages they link to are hostile by default. The client from
`build_client` refuses any host that resolves to a non-public address, on every
hop. `download` follows redirects itself, so a redirect body is never read, and
stops at a decoded byte cap and a total deadline.

An address refusal, a missed deadline, and a compressed body that ends early
are transport errors, because a Wi-Fi login page or a failing network can cause
each. Callers ask the network gate before counting one against a site.

Some servers fail only when asked for compressed bodies. When such a read fails
and the network is up, `download` asks once more for plain bytes, within the
same deadline and cap. If that works, the host gets plain requests for the life
of the process.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import zlib
from collections.abc import Awaitable, Callable

import httpx

from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)

HOST_CACHE_SECONDS = 30.0
HOST_CACHE_SIZE = 4096
MAX_HOPS = 10
ACCEPTED_ENCODINGS = "gzip, deflate"
PLAIN_ENCODING = "identity"
GZIP_MAGIC = b"\x1f\x8b"
# A request answered badly, as opposed to one that never reached the server.
RETRY_PLAIN_AFTER = (
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)
# Hosts that fail when asked for compressed bodies.
PLAIN_HOSTS: set[str] = set()
BOUNDED_CODINGS = {"gzip", "x-gzip", "deflate"}
COMPRESSED_CODINGS = BOUNDED_CODINGS | {"br", "zstd", "compress"}

Resolve = Callable[[str], Awaitable[list[str]]]
CheckHop = Callable[[httpx.Request], Awaitable[None]]
NetworkUp = Callable[[], Awaitable[bool]]


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
    """Decompresses a gzip or deflate stream up to a limit per call.

    Reads every member of a gzip stream. Bytes after the end of the compressed
    data are counted and dropped, so they cannot pile up in memory.
    """

    def __init__(self, coding: str) -> None:
        self.is_gzip = coding != "deflate"
        self.raw_allowed = coding == "deflate"
        self.window = zlib.MAX_WBITS | 16 if self.is_gzip else zlib.MAX_WBITS
        self.engine = zlib.decompressobj(self.window)
        self.started = False
        self.ended = False
        self.held = b""
        self.trailing = 0

    @property
    def complete(self) -> bool:
        """Whether the body so far is whole. An empty body counts as whole."""
        return self.ended or self.engine.eof or not (self.started or self.held)

    def _first_bytes(self, chunk: bytes, limit: int) -> bytes:
        """Tell zlib-wrapped deflate from raw. That takes the first 2 bytes."""
        self.held += chunk
        if len(self.held) < 2:
            return b""
        chunk, self.held = self.held, b""
        self.raw_allowed = False
        self.started = True
        try:
            output = self.engine.decompress(chunk, limit)
        except zlib.error:
            # Some servers send deflate without its zlib header.
            self.engine = zlib.decompressobj(-zlib.MAX_WBITS)
            output = self.engine.decompress(chunk, limit)
        if self.engine.eof:
            self._next_member(self.engine.unused_data)
        return output

    def _next_member(self, rest: bytes) -> bytes:
        """What to decode next: a new gzip member, or nothing.

        Other bytes after the end of the compressed data are only counted.
        """
        rest = self.held + rest
        self.held = b""
        if not rest:
            return b""
        if self.is_gzip and rest.startswith(GZIP_MAGIC):
            self.engine = zlib.decompressobj(self.window)
            return rest
        if self.is_gzip and rest == GZIP_MAGIC[:1]:
            self.held = rest
            return b""
        self.ended = True
        self.trailing += len(rest)
        return b""

    def feed(self, chunk: bytes, limit: int) -> bytes:
        if self.ended:
            self.trailing += len(chunk)
            return b""
        if self.raw_allowed:
            return self._first_bytes(chunk, limit)
        self.started = True
        output = bytearray()
        data = chunk
        while True:
            if self.engine.eof:
                data = self._next_member(data)
                if not data:
                    return bytes(output)
            output += self.engine.decompress(data, limit - len(output))
            if len(output) >= limit or not self.engine.eof:
                return bytes(output)
            data = self.engine.unused_data


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
        trailing = inflater.trailing if inflater else 0
        room = max_bytes - len(received) - trailing
        try:
            data = inflater.feed(chunk, room + 1) if inflater else chunk
        except zlib.error as error:
            raise httpx.DecodingError(
                str(error), request=response.request
            ) from error
        received += data
        trailing = inflater.trailing if inflater else 0
        if len(received) + trailing > max_bytes:
            raise too_large()
    if inflater and not inflater.complete:
        raise httpx.RemoteProtocolError(
            "the compressed body ended early", request=response.request
        )
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


def _remember_plain(host: str) -> None:
    if len(PLAIN_HOSTS) >= HOST_CACHE_SIZE:
        PLAIN_HOSTS.clear()
    PLAIN_HOSTS.add(host)
    log.info("host_gets_plain_requests", host=host)


async def _one_try(
    client: httpx.AsyncClient, request: httpx.Request, max_bytes: int
) -> httpx.Response | httpx.Request:
    """The response with its body, or the request a redirect leads to."""
    response = await client.send(request, stream=True, follow_redirects=False)
    try:
        following = response.next_request
        if following is not None:
            return following
        body = await _read_capped(response, max_bytes)
        return _with_body(response, body)
    finally:
        await response.aclose()


async def _one_hop(
    client: httpx.AsyncClient,
    request: httpx.Request,
    max_bytes: int,
    network_up: NetworkUp | None,
) -> httpx.Response | httpx.Request:
    """One hop, asking this host for what it is known to serve."""
    host = request.url.host
    if host in PLAIN_HOSTS:
        request.headers["Accept-Encoding"] = PLAIN_ENCODING
        return await _one_try(client, request, max_bytes)
    request.headers["Accept-Encoding"] = ACCEPTED_ENCODINGS
    try:
        return await _one_try(client, request, max_bytes)
    except RETRY_PLAIN_AFTER:
        if network_up is None or not await network_up():
            raise
    request.headers["Accept-Encoding"] = PLAIN_ENCODING
    result = await _one_try(client, request, max_bytes)
    if isinstance(result, httpx.Request) or result.status_code < 400:
        _remember_plain(host)
    return result


async def download(
    client: httpx.AsyncClient,
    url: str,
    max_bytes: int,
    deadline_seconds: float,
    headers: dict[str, str] | None = None,
    check_hop: CheckHop | None = None,
    network_up: NetworkUp | None = None,
) -> httpx.Response:
    """GET a URL, following redirects, within a byte cap and a deadline.

    `check_hop` runs before each redirect is followed, and may raise to stop.
    `network_up` says whether a failed read may be retried for plain bytes.
    """
    request = client.build_request("GET", url, headers=headers)
    try:
        async with asyncio.timeout(deadline_seconds):
            for hop in range(MAX_HOPS + 1):
                if hop and check_hop is not None:
                    await check_hop(request)
                result = await _one_hop(client, request, max_bytes, network_up)
                if isinstance(result, httpx.Response):
                    return result
                request = result
    except TimeoutError as error:
        raise DeadlineExceeded(
            f"no complete response in {deadline_seconds} s", request=request
        ) from error
    raise httpx.TooManyRedirects(
        f"more than {MAX_HOPS} redirects", request=request
    )
