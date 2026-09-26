"""SSRF-safe live checks for ``deploy`` acceptance criteria (spec §5.4, §5.6, §11 "Acceptance checks").

Only the server fetches a ``deploy`` criterion's URL, and only through
:class:`LiveChecker`:

* ``https`` only, no user-info, default port only;
* the host must be in the crew's human-set ``live_check_domains`` (exact match);
* DNS is resolved **once**; every resolved address must be public (no loopback,
  link-local ``169.254.0.0/16``, RFC 1918, CGNAT ``100.64.0.0/10``, IPv6 ULA
  ``fc00::/7``, multicast, reserved, documentation ranges, IPv4-mapped or 6to4
  forms of those, nor any extra network in ``REMEMBRA_CREW_LIVECHECK_BLOCKED_CIDRS``
  such as the Coolify internal network);
* the TCP connection goes to that pinned IP (TLS still verifies the certificate
  for the host name), so a second DNS answer cannot rebind it;
* redirects are followed only to hosts on the same allow-list and are
  re-resolved and re-checked (at most :data:`MAX_REDIRECTS`);
* 5 s total timeout; the response body is read up to 4 KB and discarded.

A check never raises for a network outcome: it returns a
:class:`LiveCheckResult` whose ``ok`` is true only for a 2xx final response.
Nothing here ever runs a criterion's ``match`` string.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, dataclass
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

DEFAULT_TIMEOUT_S: Final = 5.0
MAX_REDIRECTS: Final = 3
MAX_BODY_BYTES: Final = 4096
MAX_HEADER_BYTES: Final = 16 * 1024
MAX_HEADERS: Final = 100
MAX_URL_CHARS: Final = 512
REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
USER_AGENT: Final = "Remembra-Crew-LiveCheck/1"
BLOCKED_CIDRS_ENV: Final = "REMEMBRA_CREW_LIVECHECK_BLOCKED_CIDRS"

_BLOCKED: Final = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
        "fec0::/10",
        "ff00::/8",
    )
)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str], Awaitable[list[str]]]


def _extra_blocked() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw = os.environ.get(BLOCKED_CIDRS_ENV, "")
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return tuple(nets)


def is_public_ip(value: str) -> bool:
    """True only for a globally routable unicast address outside every blocked network."""
    try:
        ip: IPAddress = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    candidates: list[IPAddress] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            candidates.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            candidates.append(ip.sixtofour)
        if ip.teredo is not None:
            candidates.extend(ip.teredo)
    blocked = _BLOCKED + _extra_blocked()
    for candidate in candidates:
        if not candidate.is_global or candidate.is_multicast:
            return False
        if any(candidate.version == net.version and candidate in net for net in blocked):
            return False
    return True


@dataclass(frozen=True)
class LiveCheckResult:
    url: str
    ok: bool
    status: int | None = None
    error: str | None = None  # machine code; None when a response was received
    host: str | None = None
    ip: str | None = None
    redirects: int = 0
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


async def system_resolver(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    out: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in out:
            out.append(addr)
    return out


def normalise_host(host: str) -> str | None:
    try:
        return host.strip().rstrip(".").encode("idna").decode("ascii").lower() or None
    except UnicodeError:
        return None


class LiveChecker:
    """Fetches ``https`` URLs on an allow-list with a pinned, public IP. One instance per crew settings snapshot.

    ``resolver``, ``ip_allowed``, ``ssl_context`` and ``connect_port`` exist so tests can
    run the real request path against a local TLS server; production uses the defaults.
    """

    def __init__(
        self,
        allowed_hosts: Iterable[str],
        *,
        resolver: Resolver | None = None,
        ip_allowed: Callable[[str], bool] = is_public_ip,
        ssl_context: ssl.SSLContext | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_redirects: int = MAX_REDIRECTS,
        connect_port: int = 443,
    ) -> None:
        self.allowed = frozenset(h for h in (normalise_host(x) for x in allowed_hosts) if h)
        self.resolver = resolver or system_resolver
        self.ip_allowed = ip_allowed
        self.ssl_context = ssl_context or ssl.create_default_context()
        self.timeout_s = timeout_s
        self.max_redirects = max_redirects
        self.connect_port = connect_port

    def validate_url(self, url: str) -> tuple[str, str]:
        """``(host, request_target)`` for an allowed URL, else raises :class:`_Refused`."""
        if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS:
            raise _Refused("invalid_url")
        if any(ch in url for ch in "\r\n\t \x00"):
            raise _Refused("invalid_url")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as e:
            raise _Refused("invalid_url") from e
        if parts.scheme.lower() != "https":
            raise _Refused("scheme_not_https")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise _Refused("userinfo_not_allowed")
        if port not in (None, 443):
            raise _Refused("port_not_allowed")
        host = normalise_host(parts.hostname or "")
        if host is None:
            raise _Refused("invalid_url")
        try:
            ipaddress.ip_address(host.strip("[]"))
            raise _Refused("ip_literal_not_allowed")
        except ValueError:
            pass
        if host not in self.allowed:
            raise _Refused("host_not_allowed")
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        return host, target

    async def _pin(self, host: str) -> str:
        try:
            addresses = await self.resolver(host)
        except (OSError, UnicodeError) as e:
            raise _Refused("dns_failed") from e
        if not addresses:
            raise _Refused("dns_failed")
        # Every answer must be public: a mixed answer set is how rebinding sneaks a private address in.
        if not all(self.ip_allowed(a) for a in addresses):
            raise _Refused("ip_not_public")
        return addresses[0]

    async def _request(self, host: str, ip: str, target: str) -> tuple[int, dict[str, str]]:
        try:
            reader, writer = await asyncio.open_connection(ip, self.connect_port, ssl=self.ssl_context, server_hostname=host)
        except ssl.SSLError as e:
            raise _Refused("tls_failed") from e
        except OSError as e:
            raise _Refused("connect_failed") from e
        try:
            request = (
                f"GET {target} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: {USER_AGENT}\r\n"
                "Accept: */*\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n"
            )
            writer.write(request.encode("ascii", "strict"))
            await writer.drain()
            status_line = await reader.readline()
            parts = status_line.decode("latin-1").split(" ", 2)
            if len(parts) < 2 or not parts[0].startswith("HTTP/1.") or not parts[1].isdigit():
                raise _Refused("bad_response")
            status = int(parts[1])
            headers: dict[str, str] = {}
            size = 0
            while True:
                line = await reader.readline()
                size += len(line)
                if size > MAX_HEADER_BYTES or len(headers) > MAX_HEADERS:
                    raise _Refused("bad_response")
                if line in (b"\r\n", b"\n", b""):
                    break
                name, sep, value = line.decode("latin-1").partition(":")
                if sep:
                    headers[name.strip().lower()] = value.strip()
            await reader.read(MAX_BODY_BYTES)  # read at most 4 KB, then discard it
            return status, headers
        except ssl.SSLError as e:
            raise _Refused("tls_failed") from e
        except (OSError, UnicodeError) as e:
            raise _Refused("connect_failed") from e
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (TimeoutError, OSError, ssl.SSLError):
                pass

    async def _run(self, url: str, state: dict[str, Any]) -> LiveCheckResult:
        current = url
        for hop in range(self.max_redirects + 1):
            state["redirects"] = hop
            host, target = self.validate_url(current)
            state["host"] = host
            ip = await self._pin(host)
            state["ip"] = ip
            status, headers = await self._request(host, ip, target)
            state["status"] = status
            location = headers.get("location")
            if status in REDIRECT_STATUSES and location:
                current = urljoin(current, location)
                continue
            return LiveCheckResult(url, 200 <= status < 300, status, None, host, ip, hop)
        raise _Refused("too_many_redirects")

    async def check(self, url: str) -> LiveCheckResult:
        """Fetch ``url`` under every rule above; never raises for a network or policy outcome."""
        started = time.monotonic()
        state: dict[str, Any] = {"redirects": 0, "host": None, "ip": None, "status": None}

        def done(error: str) -> LiveCheckResult:
            return LiveCheckResult(
                url,
                False,
                state["status"],
                error,
                state["host"],
                state["ip"],
                state["redirects"],
                int((time.monotonic() - started) * 1000),
            )

        try:
            result = await asyncio.wait_for(self._run(url, state), timeout=self.timeout_s)
        except _Refused as e:
            return done(e.code)
        except TimeoutError:
            return done("timeout")
        return LiveCheckResult(
            result.url,
            result.ok,
            result.status,
            None,
            result.host,
            result.ip,
            result.redirects,
            int((time.monotonic() - started) * 1000),
        )
