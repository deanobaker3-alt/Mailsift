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
        return type("R", (), {"status": self.status})()

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
