"""Safe one-click unsubscribe requests (RFC 8058).

The unsubscribe URL comes from an email, so it's attacker-controllable, and
MailSift runs inside your home network. A malicious header could try to make
it talk to your router or another device (SSRF). So:

  * HTTPS on port 443 only, no credentials in the URL;
  * the host must resolve ONLY to public internet addresses;
  * the connection is then made to that exact checked IP (with the real
    hostname used for TLS/SNI and certificate checks), so a DNS answer that
    changes between "check" and "connect" can't sneak past (DNS rebinding);
  * no redirects are followed, no cookies or credentials are sent.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlencode, urlsplit

import certifi
import urllib3

USER_AGENT = "MailSift/1.1 (RFC 8058 one-click unsubscribe)"


class UnsafeURL(Exception):
    pass


def check_url(url: str, resolver=socket.getaddrinfo) -> tuple[str, str]:
    """Returns (hostname, checked_ip) or raises UnsafeURL."""
    try:
        parts = urlsplit(url)
        port = parts.port or 443
    except ValueError as exc:
        raise UnsafeURL("Malformed link") from exc
    if parts.scheme.lower() != "https":
        raise UnsafeURL("Only secure (https) unsubscribe links are used")
    if parts.username or parts.password:
        raise UnsafeURL("Link contains login details")
    if port != 443:
        raise UnsafeURL("Link uses an unusual port")
    host = parts.hostname
    if not host:
        raise UnsafeURL("Link has no host")
    try:
        infos = resolver(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeURL("Couldn't look up the sender's server") from exc
    if not infos:
        raise UnsafeURL("Couldn't look up the sender's server")
    addresses = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%")[0])
        if not address.is_global:
            raise UnsafeURL("Link points at a private or local address")
        addresses.append(str(address))
    return host, addresses[0]


def _pinned_pool(ip: str, host: str, timeout: float):
    return urllib3.HTTPSConnectionPool(
        ip, 443,
        server_hostname=host, assert_hostname=host,       # TLS still verifies the real name
        cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(),
        timeout=urllib3.Timeout(total=timeout), retries=False, maxsize=1,
    )


def one_click_post(url: str, timeout=15, resolver=socket.getaddrinfo, pool_factory=_pinned_pool):
    """Returns (ok, detail)."""
    try:
        host, ip = check_url(url, resolver)
    except UnsafeURL as exc:
        return False, str(exc)
    parts = urlsplit(url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    try:
        pool = pool_factory(ip, host, timeout)
        try:
            response = pool.request(
                "POST", target,
                body=urlencode({"List-Unsubscribe": "One-Click"}),
                headers={"Host": host, "User-Agent": USER_AGENT,
                         "Content-Type": "application/x-www-form-urlencoded"},
                redirect=False,
            )
        finally:
            pool.close()
    except Exception as exc:  # TLS failure, timeout, refused...
        return False, f"Couldn't reach the sender ({exc.__class__.__name__})"
    if response.status < 400:
        return True, f"HTTP {response.status}"
    return False, f"Sender's server replied HTTP {response.status}"
