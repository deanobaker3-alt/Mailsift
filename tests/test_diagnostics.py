"""The self-check. The network is faked here (these tests must run offline);
the real thing is exercised by running the diagnostic itself."""
import ssl

import pytest

from mailsift import diagnostics
from mailsift.demo import DemoGraph
from mailsift.netguard import UnsafeURL
from mailsift.store import Store

ISSUER = {"issuer": ((("organizationName", "Example Inspecting Proxy"),),)}


class FakeNet:
    """First handshake uses MailSift's trust store, the second certifi only."""

    def __init__(self, resolve=None, trust=None, certifi_only=None, request=None):
        self.outcomes = {"resolve": resolve, "trust": trust, "certifi": certifi_only, "request": request}
        self.handshakes = 0
        self.calls = []

    def _maybe_raise(self, key):
        if isinstance(self.outcomes[key], Exception):
            raise self.outcomes[key]

    def resolve(self, host):
        self.calls.append("resolve")
        self._maybe_raise("resolve")
        return host, "93.184.216.34"

    def handshake(self, ip, host, context):
        self.handshakes += 1
        key = "trust" if self.handshakes == 1 else "certifi"
        self.calls.append(f"handshake:{key}")
        self._maybe_raise(key)
        return ISSUER

    def pinned_request(self, ip, host):
        self.calls.append("request")
        self._maybe_raise("request")
        return 200


class FakeAuth:
    cache_encrypted = True

    def __init__(self, signed_in=True, scopes=frozenset(diagnostics.NEEDED_SCOPES)):
        self._signed_in, self._scopes = signed_in, scopes

    def signed_in(self):
        return self._signed_in

    def granted_scopes(self):
        return set(self._scopes) if self._scopes is not None else None


def statuses(report):
    return {c["name"]: c["status"] for c in report["checks"]}


@pytest.fixture
def world():
    return DemoGraph(delay=0), FakeAuth(), Store(":memory:")


def test_everything_working(world):
    graph, auth, store = world
    report = diagnostics.run(graph, auth, store, net=FakeNet())
    assert report["problems"] == 0
    assert set(statuses(report).values()) <= {"ok", "info"}
    assert statuses(report)["Secure connections (one-click unsubscribe)"] == "ok"


def test_https_inspection_is_detected_and_reported_as_handled(world):
    # The exact situation that broke one-click before v1.7.0: certifi alone
    # rejects the certificate, this computer's own trust store accepts it.
    graph, auth, store = world
    net = FakeNet(certifi_only=ssl.SSLCertVerificationError(1, "certificate verify failed"))
    report = diagnostics.run(graph, auth, store, net=net)
    check = next(c for c in report["checks"] if c["name"].startswith("Secure connections"))
    assert check["status"] == "info"
    assert "Example Inspecting Proxy" in check["detail"]
    assert report["problems"] == 0, "inspection that MailSift handles isn't a problem"


def test_tls_failing_even_with_system_certificates_is_a_problem(world):
    graph, auth, store = world
    net = FakeNet(trust=ssl.SSLError("handshake failure"))
    report = diagnostics.run(graph, auth, store, net=net)
    assert statuses(report)["Secure connections (one-click unsubscribe)"] == "fail"
    assert report["problems"] == 1


def test_blocked_connection_and_failed_lookup_are_problems(world):
    graph, auth, store = world
    for net in (FakeNet(request=ConnectionRefusedError()), FakeNet(resolve=UnsafeURL("lookup failed"))):
        assert statuses(diagnostics.run(graph, auth, store, net=net))[
            "Secure connections (one-click unsubscribe)"] == "fail"


def test_the_exact_unsubscribe_connection_is_exercised(world):
    # The point of the whole feature: test the real pinned code path, not a
    # stand-in. If this stops being called, the check proves nothing.
    graph, auth, store = world
    net = FakeNet()
    diagnostics.run(graph, auth, store, net=net)
    assert "request" in net.calls


def test_missing_permission_names_the_feature_it_breaks(world):
    graph, _, store = world
    auth = FakeAuth(scopes=set(diagnostics.NEEDED_SCOPES) - {"mailboxsettings.readwrite"})
    report = diagnostics.run(graph, auth, store, net=FakeNet())
    perm = next(c for c in report["checks"] if c["name"] == "Permissions")
    assert perm["status"] == "fail" and "blocking" in perm["detail"]


def test_unknown_permissions_are_skipped_not_failed(world):
    graph, _, store = world
    report = diagnostics.run(graph, FakeAuth(scopes=None), store, net=FakeNet())
    assert "Permissions" not in statuses(report)
    assert report["problems"] == 0


def test_not_signed_in_still_checks_the_rest(world):
    graph, _, store = world
    report = diagnostics.run(graph, FakeAuth(signed_in=False), store, net=FakeNet())
    s = statuses(report)
    assert s["Microsoft sign-in"] == "fail"
    assert s["Local history file"] == "ok"
    assert s["Secure connections (one-click unsubscribe)"] == "ok"


def test_demo_mode_uses_no_network(world):
    graph, auth, store = world
    net = FakeNet()
    report = diagnostics.run(graph, auth, store, demo=True, net=net)
    assert net.calls == []
    assert statuses(report)["Secure connections (one-click unsubscribe)"] == "info"


def test_no_check_mentions_the_users_address(world):
    # The report is meant to be pasted to whoever gave someone the app.
    graph, auth, store = world
    report = diagnostics.run(graph, auth, store, net=FakeNet())
    text = " ".join(c["detail"] for c in report["checks"])
    assert "@" not in text
