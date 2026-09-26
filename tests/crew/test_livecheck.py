"""SSRF-safe live checks (§5.6, §13.1 "SSRF fetcher") against a real local TLS server.

The request path is the production one (pinned-IP TCP connect, TLS with host-name
verification, HTTP/1.1, redirects re-validated). Tests inject a resolver and, for the
positive cases only, an IP policy that admits 127.0.0.1 so the local server is reachable.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from remembra.crew.livecheck import LiveChecker, is_public_ip


def make_tls_material(tmp_path: Path, hosts: list[str]) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """(server context, client context trusting only the test CA) for ``hosts``."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Remembra Test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = [x509.DNSName(h) for h in hosts]
    sans.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = tmp_path / "leaf.pem"
    key_path = tmp_path / "leaf.key"
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server.load_cert_chain(str(cert_path), str(key_path))
    client = ssl.create_default_context(cadata=ca_cert.public_bytes(serialization.Encoding.PEM).decode())
    return server, client


HOSTS = ["live.example.com", "other.example.com", "private.example.com", "evil.example.net"]


@dataclass
class Server:
    port: int
    requests: list[tuple[str, str]] = field(default_factory=list)  # (host header, path)


@asynccontextmanager
async def tls_server(tmp_path) -> AsyncIterator[tuple[Server, object]]:
    server_ctx, client_ctx = make_tls_material(tmp_path, HOSTS)
    state = Server(port=0)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await reader.readline()).decode()
            host = ""
            while True:
                h = await reader.readline()
                if h in (b"\r\n", b"", b"\n"):
                    break
                name, _, value = h.decode().partition(":")
                if name.lower() == "host":
                    host = value.strip()
            path = line.split(" ")[1] if " " in line else "/"
            state.requests.append((host, path))
            if path == "/hang":
                await asyncio.sleep(5)
                return
            routes = {
                "/ok": (200, {}, b"x" * 10_000),
                "/fail": (503, {}, b"down"),
                "/redir-ok": (302, {"Location": "https://other.example.com/ok"}, b""),
                "/redir-bad": (302, {"Location": "https://evil.example.net/ok"}, b""),
                "/redir-private": (301, {"Location": "https://private.example.com/ok"}, b""),
                "/redir-http": (302, {"Location": "http://other.example.com/ok"}, b""),
                "/loop": (302, {"Location": "/loop"}, b""),
            }
            code, headers, body = routes.get(path, (404, {}, b"nope"))
            head = f"HTTP/1.1 {code} X\r\nContent-Length: {len(body)}\r\nConnection: close\r\n"
            head += "".join(f"{k}: {v}\r\n" for k, v in headers.items())
            writer.write(head.encode() + b"\r\n" + body)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
    state.port = srv.sockets[0].getsockname()[1]
    try:
        yield state, client_ctx
    finally:
        srv.close()


def resolver_for(mapping: dict[str, list[str]], calls: list[str]):
    async def resolve(host: str) -> list[str]:
        calls.append(host)
        return mapping.get(host, [])

    return resolve


LOCAL_OK = lambda ip: ip == "127.0.0.1" or is_public_ip(ip)  # noqa: E731


def checker(
    server: Server,
    ctx,
    *,
    allowed=("live.example.com", "other.example.com", "private.example.com"),
    mapping=None,
    calls=None,
    **kw,
):
    mapping = mapping or {
        "live.example.com": ["127.0.0.1"],
        "other.example.com": ["127.0.0.1"],
        "private.example.com": ["10.0.0.7"],
    }
    return LiveChecker(
        allowed,
        resolver=resolver_for(mapping, calls if calls is not None else []),
        ip_allowed=kw.pop("ip_allowed", LOCAL_OK),
        ssl_context=ctx,
        connect_port=server.port,
        **kw,
    )


async def test_2xx_is_ok_body_is_capped_and_ip_is_pinned(tmp_path):
    async with tls_server(tmp_path) as (srv, ctx):
        calls: list[str] = []
        res = await checker(srv, ctx, calls=calls).check("https://live.example.com/ok")
        assert res.ok and res.status == 200 and res.error is None
        assert res.ip == "127.0.0.1" and res.host == "live.example.com" and res.redirects == 0
        assert calls == ["live.example.com"]  # resolved exactly once
        assert srv.requests == [("live.example.com", "/ok")]


async def test_non_2xx_is_not_ok(tmp_path):
    async with tls_server(tmp_path) as (srv, ctx):
        res = await checker(srv, ctx).check("https://live.example.com/fail")
        assert not res.ok and res.status == 503 and res.error is None


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://live.example.com/ok", "scheme_not_https"),
        ("https://unlisted.example.com/ok", "host_not_allowed"),
        ("https://live.example.com:8443/ok", "port_not_allowed"),
        ("https://user:pw@live.example.com/ok", "userinfo_not_allowed"),
        ("https://169.254.169.254/latest/meta-data", "ip_literal_not_allowed"),
        ("https://[::1]/", "ip_literal_not_allowed"),
        ("ftp://live.example.com/", "scheme_not_https"),
        ("https://live.example.com/a b", "invalid_url"),
        ("https://live.example.com/\r\nHost: evil", "invalid_url"),
    ],
)
async def test_refused_before_any_network(tmp_path, url, code):
    async with tls_server(tmp_path) as (srv, ctx):
        calls: list[str] = []
        res = await checker(srv, ctx, calls=calls).check(url)
        assert not res.ok and res.error == code
        assert calls == [] and srv.requests == []


@pytest.mark.parametrize(
    "answers",
    [
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["10.0.0.7"],
        ["172.20.1.1"],
        ["192.168.1.2"],
        ["100.64.3.4"],
        ["fc00::1"],
        ["::ffff:127.0.0.1"],
        ["0.0.0.0"],
        ["93.184.216.34", "127.0.0.1"],
    ],
)
async def test_private_and_mixed_dns_answers_are_refused_with_the_default_policy(tmp_path, answers):
    async with tls_server(tmp_path) as (srv, ctx):
        c = LiveChecker(
            ["live.example.com"], resolver=resolver_for({"live.example.com": answers}, []), ssl_context=ctx, connect_port=srv.port
        )
        res = await c.check("https://live.example.com/ok")
        assert not res.ok and res.error == "ip_not_public"
        assert srv.requests == []


async def test_dns_failure(tmp_path):
    async with tls_server(tmp_path) as (srv, ctx):
        res = await checker(srv, ctx, mapping={"live.example.com": []}).check("https://live.example.com/ok")
        assert res.error == "dns_failed"


async def test_redirect_to_allowed_host_is_followed_and_re_resolved(tmp_path):
    async with tls_server(tmp_path) as (srv, ctx):
        calls: list[str] = []
        res = await checker(srv, ctx, calls=calls).check("https://live.example.com/redir-ok")
        assert res.ok and res.redirects == 1 and res.host == "other.example.com"
        assert calls == ["live.example.com", "other.example.com"]
        assert srv.requests == [("live.example.com", "/redir-ok"), ("other.example.com", "/ok")]


async def test_redirects_off_the_allow_list_to_private_ips_or_http_are_refused(tmp_path):
    async with tls_server(tmp_path) as (srv, ctx):
        c = checker(srv, ctx)
        bad = await c.check("https://live.example.com/redir-bad")
        assert not bad.ok and bad.error == "host_not_allowed"
        private = await c.check("https://live.example.com/redir-private")
        assert not private.ok and private.error == "ip_not_public"
        plain = await c.check("https://live.example.com/redir-http")
        assert not plain.ok and plain.error == "scheme_not_https"
        assert all(host == "live.example.com" for host, _ in srv.requests)


async def test_redirect_loop_stops(tmp_path):
    async with tls_server(tmp_path) as (srv, ctx):
        res = await checker(srv, ctx).check("https://live.example.com/loop")
        assert res.error == "too_many_redirects" and len(srv.requests) == 4


async def test_timeout(tmp_path):
    async with tls_server(tmp_path) as (srv, ctx):
        res = await checker(srv, ctx, timeout_s=0.3).check("https://live.example.com/hang")
        assert not res.ok and res.error == "timeout"


async def test_tls_name_mismatch_fails(tmp_path):
    async with tls_server(tmp_path) as (srv, ctx):
        c = checker(srv, ctx, allowed=("unknown-name.example.org",), mapping={"unknown-name.example.org": ["127.0.0.1"]})
        res = await c.check("https://unknown-name.example.org/ok")
        assert not res.ok and res.error == "tls_failed"


@pytest.mark.parametrize(
    ("ip", "public"),
    [
        ("93.184.216.34", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.0.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("100.127.255.254", False),
        ("fc00::1", False),
        ("fd12:3456::1", False),
        ("fe80::1", False),
        ("::1", False),
        ("::ffff:10.0.0.1", False),
        ("2002:0a00:0001::1", False),  # 6to4 wrapping 10.0.0.1
        ("224.0.0.1", False),
        ("198.51.100.7", False),
        ("not-an-ip", False),
    ],
)
def test_is_public_ip(ip, public):
    assert is_public_ip(ip) is public


def test_extra_blocked_networks_from_env(monkeypatch):
    assert is_public_ip("93.184.216.34")
    monkeypatch.setenv("REMEMBRA_CREW_LIVECHECK_BLOCKED_CIDRS", "93.184.216.0/24")
    assert not is_public_ip("93.184.216.34")
