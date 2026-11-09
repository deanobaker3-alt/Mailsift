"""The public docs make specific, checkable promises. A security document
that describes a control the code doesn't have is worse than no document,
because people rely on it. These tests fail when the two drift apart.

They deliberately check the *claim*, not the wording: each one looks for the
mechanism in the code, so rephrasing a doc is free but quietly removing a
control is not.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "mailsift"


def code(*names) -> str:
    return "\n".join((PKG / n).read_text(encoding="utf-8") for n in names)


def doc(name) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


# (what the docs promise, where the mechanism must live, how to recognise it)
PROMISES = [
    ("request bodies are capped",        ("app.py",),        r"512\s*\*\s*1024|524288"),
    ("framing is refused",               ("app.py",),        r"X-Frame-Options|frame-ancestors 'none'"),
    ("no inline script is allowed",      ("app.py",),        r"script-src 'self'"),
    ("CSRF needs a header token",        ("app.py",),        r"X-MailSift-Token"),
    ("the session cookie is strict",     ("app.py",),        r"(?is)samesite.{0,20}strict"),
    ("the launch key expires",           ("app.py", "launcher.py"), r"\b120\b|2\s*\*\s*60"),
    ("tokens never go to a non-Graph host", ("graph.py",),   r"Refusing to send credentials"),
    ("blocks match the exact sender",    ("blocklist.py",),  r"fromAddresses"),
    ("one-click refuses redirects",      ("netguard.py",),   r"redirect\s*=\s*False"),
    ("one-click is port 443 only",       ("netguard.py",),   r"\b443\b"),
    ("one-click refuses private addresses", ("netguard.py",), r"is_global"),
    ("the sender's mailto text is ignored", ("headers.py",), r"deliberately ignored"),
    ("header parsing is bounded",        ("headers.py",),    r"MAX_HEADER"),
    ("it quits itself when idle",        ("__main__.py",),   r"IDLE_EXIT_SECONDS\s*=\s*10\s*\*\s*60"),
]


@pytest.mark.parametrize("promise,files,pattern", PROMISES)
def test_the_docs_promise_something_the_code_does(promise, files, pattern):
    assert re.search(pattern, code(*files)), f"the docs promise: {promise}"


def test_suspicious_senders_can_only_ever_be_blocked():
    """README and SECURITY.md both say this is enforced server-side."""
    from mailsift.classify import ALLOWED_ACTIONS
    for kind in ("dodgy", "unverified", "none"):
        assert ALLOWED_ACTIONS[kind] == {"keep", "block"}
    assert "ALLOWED_ACTIONS" in code("actions.py"), "the table exists but nothing enforces it"


def test_the_scan_never_asks_for_message_bodies():
    """'Headers only' is the central privacy claim."""
    assert not re.search(r"\$select[^\n]*\bbody\b", code("scanner.py", "graph.py"))


def test_the_permissions_table_matches_what_is_requested():
    """The README lists four permissions and says why. Asking for more than
    it lists would make that table a lie."""
    asked = {s.lower() for s in re.findall(
        r"(User\.Read|Mail\.ReadWrite|Mail\.Send|MailboxSettings\.ReadWrite)", code("auth.py"), re.I)}
    listed = {s.lower() for s in re.findall(
        r"`(User\.Read|Mail\.ReadWrite|Mail\.Send|MailboxSettings\.ReadWrite)`", doc("README.md"))}
    assert asked == listed == {"user.read", "mail.readwrite", "mail.send", "mailboxsettings.readwrite"}


def test_the_thresholds_the_docs_quote_are_the_real_ones():
    from mailsift.score import PICK, SUGGEST
    assert PICK == 70 and SUGGEST == 45
    for name in ("README.md", "USER_GUIDE.txt"):
        assert "70" in doc(name) and "45" in doc(name), f"{name} quotes the thresholds"


def test_every_bundled_font_is_named_in_the_licensing_page():
    """The fonts ship inside the .exe, so leaving one out of LICENSING.md is
    a licence problem, not a typo. IBM Plex and B612 were missing once."""
    families = set()
    for f in (PKG / "static" / "fonts").glob("*.woff2"):
        families.add(re.split(r"-(latin|mono|\d)", f.stem)[0].replace("-", " "))
    page = doc("LICENSING.md").lower()
    for family in families:
        key = family.split()[0]
        assert key in page, f"{family} is bundled but not named in LICENSING.md"


def test_every_module_appears_in_the_code_map():
    mapped = doc("docs/BUILDING.md")
    for path in sorted(PKG.glob("*.py")):
        if path.name.startswith("__"):
            continue
        assert path.name in mapped, f"{path.name} is missing from the code map"
