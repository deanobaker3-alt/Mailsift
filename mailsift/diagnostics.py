""""Check my setup": proves, on this machine, that each thing MailSift relies
on actually works.

Born of a bug that every unit test missed. One-click unsubscribes failed on
any machine whose network or antivirus inspects HTTPS, for days, while 100+
tests passed, because the tests faked the connection. So these checks use the
real code paths against the real network: the same pinned, certificate-checked
connection a one-click unsubscribe uses, and the same Graph calls a clean-up
makes.

Privacy: the only hosts contacted are Microsoft's, which MailSift already
talks to. Nothing is posted anywhere, nothing in the mailbox is changed, and
results stay on this computer.
"""
from __future__ import annotations

import socket
import ssl
import time

import certifi

from . import __version__
from .netguard import USER_AGENT, UnsafeURL, _pinned_pool, _trust_store, check_url

# A Microsoft host, so the check adds no new third party. Deliberately not
# graph.microsoft.com: corporate proxies commonly exempt Microsoft 365
# endpoints from HTTPS inspection, which would hide exactly what we're
# looking for. Unsubscribe links go to ordinary websites, so test one.
TLS_CHECK_HOST = "www.microsoft.com"
TIMEOUT = 8

# What each permission is for, so a missing one says which feature breaks.
NEEDED_SCOPES = {
    "user.read": "showing which account is signed in",
    "mail.readwrite": "scanning and moving old mail",
    "mail.send": "unsubscribing from senders that only accept email",
    "mailboxsettings.readwrite": "blocking senders (the inbox rule)",
}


def _check(name, status, detail):
    return {"name": name, "status": status, "detail": detail}


def _issuer_org(cert: dict) -> str:
    for rdn in cert.get("issuer", ()):
        for key, value in rdn:
            if key in ("organizationName", "commonName"):
                return value
    return ""


class Network:
    """The real network operations, kept separate so tests can replace them."""

    def __init__(self, resolver=socket.getaddrinfo, timeout=TIMEOUT):
        self.resolver = resolver
        self.timeout = timeout

    def resolve(self, host):
        return check_url(f"https://{host}/", self.resolver)

    def handshake(self, ip, host, context):
        """TLS handshake only. Returns the server's certificate as seen."""
        with socket.create_connection((ip, 443), timeout=self.timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                return tls.getpeercert() or {}

    def pinned_request(self, ip, host):
        """The exact connection a one-click unsubscribe makes, as a HEAD."""
        pool = _pinned_pool(ip, host, self.timeout)
        try:
            response = pool.request("HEAD", "/", headers={"Host": host, "User-Agent": USER_AGENT},
                                    redirect=False, preload_content=False)
            try:
                return response.status
            finally:
                response.close()
        finally:
            pool.close()


def check_secure_connections(net: Network, host=TLS_CHECK_HOST) -> list[dict]:
    name = "Secure connections (one-click unsubscribe)"
    try:
        host, ip = net.resolve(host)
    except UnsafeURL:
        return [_check(name, "fail",
                       f"Couldn't look up {host}, so this computer can't reach the internet right now, "
                       "or something is blocking it. One-click unsubscribes need it.")]

    try:
        cert = net.handshake(ip, host, _trust_store())
        net.pinned_request(ip, host)
    except ssl.SSLError as exc:
        return [_check(name, "fail",
                       "Secure connections fail even using this computer's own certificates "
                       f"({exc.__class__.__name__}). One-click unsubscribes won't work here; "
                       "email unsubscribes and blocking still will.")]
    except OSError as exc:
        return [_check(name, "fail",
                       f"Couldn't connect to {host} ({exc.__class__.__name__}). A firewall may be blocking "
                       "outgoing connections. One-click unsubscribes won't work until it's allowed.")]

    # Would certifi alone (what MailSift used before v1.7.0) have accepted it?
    # If not, something on this machine or network is re-signing HTTPS.
    inspected = False
    try:
        net.handshake(ip, host, ssl.create_default_context(cafile=certifi.where()))
    except ssl.SSLCertVerificationError:
        inspected = True
    except (ssl.SSLError, OSError):
        pass  # inconclusive: the main check already passed, so don't alarm anyone

    if inspected:
        by = _issuer_org(cert)
        return [_check(name, "info",
                       "Your network or antivirus inspects secure connections"
                       + (f" (certificates are re-signed by “{by}”)" if by else "")
                       + ". MailSift trusts this computer's certificates, so one-click unsubscribes work. "
                       "Versions before 1.7.0 failed here with SSLError.")]
    return [_check(name, "ok", "Working. One-click unsubscribes can reach senders directly.")]


def check_microsoft(graph, auth) -> list[dict]:
    out = []
    if auth is None:
        return [_check("Microsoft sign-in", "fail", "This copy has no app ID configured.")]
    try:
        signed_in = auth.signed_in()
    except Exception as exc:  # never let one check stop the rest
        signed_in, why = False, exc.__class__.__name__
    else:
        why = None
    if not signed_in:
        return [_check("Microsoft sign-in", "fail",
                       "Not signed in" + (f" ({why})" if why else "") + ". Sign in, then run this again.")]

    started = time.monotonic()
    try:
        graph.me()
        ms = round((time.monotonic() - started) * 1000)
        out.append(_check("Microsoft sign-in", "ok", f"Signed in, and Microsoft answered in {ms} ms."))
    except Exception as exc:
        return [_check("Microsoft sign-in", "fail",
                       f"Signed in, but Microsoft didn't answer ({getattr(exc, 'message', exc.__class__.__name__)}).")]

    granted = getattr(auth, "granted_scopes", lambda: None)()
    if granted is not None:
        missing = [scope for scope in NEEDED_SCOPES if scope not in granted]
        if missing:
            out.append(_check("Permissions", "fail",
                              "Missing permission for " + "; ".join(NEEDED_SCOPES[s] for s in missing)
                              + ". Sign out and back in, and accept every permission."))
        else:
            out.append(_check("Permissions", "ok", "Every permission MailSift needs was granted."))

    try:
        inbox = graph.well_known_folder_id("inbox")
        total = graph.count_messages()
        if not inbox:
            raise LookupError("no inbox")
        out.append(_check("Reading the mailbox", "ok",
                          "Inbox readable" + (f", {total:,} emails in the mailbox." if total is not None else ".")))
    except Exception as exc:
        out.append(_check("Reading the mailbox", "fail",
                          f"Couldn't read the inbox ({getattr(exc, 'message', exc.__class__.__name__)}). Scans won't work."))

    try:
        rules = graph.list_rules()
        ours = sum(1 for r in rules if (r.get("displayName") or "").startswith("MailSift blocklist"))
        out.append(_check("Inbox rules (blocking)", "ok",
                          f"Readable: {len(rules)} rule{'s' if len(rules) != 1 else ''}, "
                          f"{ours} of them MailSift's blocklist."))
    except Exception as exc:
        out.append(_check("Inbox rules (blocking)", "fail",
                          f"Couldn't read inbox rules ({getattr(exc, 'message', exc.__class__.__name__)}). Blocking won't work."))
    return out


def check_this_computer(auth, store, demo) -> list[dict]:
    out = []
    if demo:
        out.append(_check("Sign-in storage", "info", "Demo mode: nothing is stored."))
    elif auth is not None:
        if getattr(auth, "cache_encrypted", False):
            out.append(_check("Sign-in storage", "ok", "Encrypted by Windows. Never written in plain text."))
        else:
            out.append(_check("Sign-in storage", "info",
                              "No encrypted store available, so sign-in is kept in memory only and "
                              "you'll sign in each launch. Safe, just less convenient."))
    try:
        verdict = store.health()
        out.append(_check("Local history file", "ok" if verdict == "ok" else "fail",
                          "Readable and intact." if verdict == "ok"
                          else f"Damaged ({verdict}). “Forget me” will reset it."))
    except Exception as exc:
        out.append(_check("Local history file", "fail", f"Couldn't open it ({exc.__class__.__name__})."))
    out.append(_check("Version", "info", f"MailSift {__version__}"))
    return out


def run(graph, auth, store, demo=False, net: Network | None = None) -> dict:
    checks = []
    if demo:
        checks.append(_check("Secure connections (one-click unsubscribe)", "info",
                             "Skipped in demo mode: no network is used."))
    else:
        checks += check_secure_connections(net or Network())
    checks += check_microsoft(graph, auth)
    checks += check_this_computer(auth, store, demo)
    problems = sum(1 for c in checks if c["status"] == "fail")
    return {"checks": checks, "problems": problems, "version": __version__}
