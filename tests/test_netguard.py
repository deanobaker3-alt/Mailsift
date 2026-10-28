import socket

import pytest

from mailsift.netguard import UnsafeURL, check_url, one_click_post


def resolver_for(*ips):
    def resolve(host, port, proto=None):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    return resolve


def test_accepts_public_https():
    assert check_url("https://unsub.example.com/x", resolver_for("93.184.216.34")) == ("unsub.example.com", "93.184.216.34")


@pytest.mark.parametrize("url", [
    "http://unsub.example.com/x",
    "https://user:pass@unsub.example.com/x",
    "https://unsub.example.com:8443/x",
    "ftp://unsub.example.com/x",
])
def test_rejects_unsafe_shapes(url):
    with pytest.raises(UnsafeURL):
        check_url(url, resolver_for("93.184.216.34"))


@pytest.mark.parametrize("ip", ["127.0.0.1", "192.168.1.1", "10.0.0.5", "169.254.169.254", "::1", "fe80::1%eth0", "100.64.0.1"])
def test_rejects_private_targets(ip):
    with pytest.raises(UnsafeURL):
        check_url("https://evil.example.com/x", resolver_for(ip))


def test_rejects_if_any_address_is_private():
    # a DNS answer mixing public and private addresses is a classic rebinding setup
    with pytest.raises(UnsafeURL):
        check_url("https://evil.example.com/x", resolver_for("93.184.216.34", "192.168.0.1"))


class FakePool:
    def __init__(self, status=202):
        self.status = status
        self.calls = []
        self.closed = False

    def request(self, method, target, **kwargs):
        self.calls.append((method, target, kwargs))
        self.response = FakeResponse(self.status)
        return self.response

    def close(self):
        self.closed = True


class FakeResponse:
    """Shaped like a urllib3 response asked for with preload_content=False:
    the body is NOT available, and it has to be closed."""

    def __init__(self, status):
        self.status = status
        self.closed = False

    def close(self):
        self.closed = True


def test_post_is_pinned_to_checked_ip_and_shaped_per_rfc():
    pool = FakePool()
    seen = {}

    def factory(ip, host, timeout):
        seen.update(ip=ip, host=host)
        return pool

    ok, detail = one_click_post("https://unsub.example.com/u?id=1", resolver=resolver_for("93.184.216.34"),
                                pool_factory=factory)
    assert ok and detail == "HTTP 202"
    assert seen == {"ip": "93.184.216.34", "host": "unsub.example.com"}
    method, target, kwargs = pool.calls[0]
    assert method == "POST" and target == "/u?id=1"
    assert kwargs["body"] == "List-Unsubscribe=One-Click"
    assert kwargs["headers"]["Host"] == "unsub.example.com"
    assert "Cookie" not in kwargs["headers"] and "Authorization" not in kwargs["headers"]
    assert kwargs["redirect"] is False
    assert pool.closed


def test_post_refuses_private_without_connecting():
    def factory(*_):
        raise AssertionError("must not connect")
    ok, detail = one_click_post("https://router.example.com/x", resolver=resolver_for("192.168.0.1"), pool_factory=factory)
    assert not ok and "private" in detail


def test_server_error_reported():
    ok, detail = one_click_post("https://unsub.example.com/x", resolver=resolver_for("93.184.216.34"),
                                pool_factory=lambda *_: FakePool(500))
    assert not ok and "500" in detail


def test_hostile_reply_body_is_never_buffered_into_memory():
    # A sender's unsubscribe endpoint is attacker-controlled. Only the status
    # code is wanted, so urllib3 must not read the reply, however big it is,
    # and the response has to be closed so the connection is dropped.
    pool = FakePool(202)
    ok, _ = one_click_post("https://unsub.example.com/u", resolver=resolver_for("93.184.216.34"),
                           pool_factory=lambda *_: pool)
    assert ok
    _method, _target, kwargs = pool.calls[0]
    assert kwargs["preload_content"] is False
    assert pool.response.closed and pool.closed


def test_trust_store_uses_the_os_roots_as_well_as_certifi():
    # certifi alone fails on any machine where antivirus or a filtering proxy
    # re-signs HTTPS with a locally-trusted CA, which made every one-click
    # unsubscribe fail with an SSL error while the browser opened the same
    # link fine. Verification itself must stay strict.
    import ssl

    from mailsift.netguard import _trust_store
    ctx = _trust_store()
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.get_ca_certs(), "no root certificates loaded at all"


def test_pinned_connection_still_rejects_a_certificate_for_another_name(tmp_path):
    """The whole point of pinning is connecting to a checked IP while still
    proving the server is who the link claimed. Served locally with a
    throwaway certificate, because an intercepting proxy will happily mint one
    for any name asked of it and would hide a regression here."""
    import datetime
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import urllib3
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "real.example.com")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("real.example.com")]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "c.pem", tmp_path / "k.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.TraditionalOpenSSL,
                                           serialization.NoEncryption()))

    class Quiet(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Quiet)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_file, key_file)
    server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def fetch(expected_name):
        ctx = ssl.create_default_context(cafile=str(cert_file))
        ctx.check_hostname, ctx.verify_mode = True, ssl.CERT_REQUIRED
        pool = urllib3.HTTPSConnectionPool("127.0.0.1", port, server_hostname=expected_name,
                                           assert_hostname=expected_name, ssl_context=ctx,
                                           timeout=urllib3.Timeout(total=5), retries=False)
        try:
            return pool.request("GET", "/", preload_content=False).status
        finally:
            pool.close()

    try:
        assert fetch("real.example.com") == 200
        with pytest.raises(Exception):
            fetch("evil.example.com")
    finally:
        server.shutdown()
