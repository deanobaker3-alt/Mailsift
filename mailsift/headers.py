"""Parsing for the email headers MailSift relies on."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

# Bounded for the same reason as _EMAIL below. With a plain "*" inside, a
# header of unclosed "<" makes the engine scan to the end from every one of
# them: 20,000 of them took 1.5 seconds, and List-Unsubscribe is written by
# the sender. 2048 is well past the longest real unsubscribe URL.
_ANGLE = re.compile(r"<([^>]{0,2048})>")
_AUTH = re.compile(r"\b(spf|dkim|dmarc|compauth)\s*=\s*([a-zA-Z]+)", re.IGNORECASE)
# Bounded on purpose. With plain "+" on both sides of the @, a long run of
# characters containing no @ makes the engine rescan from every position:
# a 100,000-character From header took 48 seconds to reject, and a sender
# chooses that header.
#
# The bounds are deliberately NOT the RFC's. Tried RFC 5321's 64-character
# local part first and it rejected a real Mailchimp unsubscribe address,
# which uses 66. Bulk senders encode a subscriber id in the local part and
# are not reading the spec. 256 keeps the match linear, which is the whole
# point, with room for anything an ESP is plausibly going to do.
_EMAIL = re.compile(r"[^@\s,;<>]{1,256}@[^@\s,;<>]{1,255}\.[^@\s,;<>]{1,63}")

# Belt and braces for the same attack. A header this long is not hiding a
# real address, it is trying to make the matcher work. Refused outright
# rather than truncated: a truncated match would be a *different* address,
# and these go into inbox rules.
MAX_HEADER = 4096


def header_map(headers) -> dict[str, list[str]]:
    """Graph's [{name, value}] list -> {lowercase name: [values in message order]}."""
    out: dict[str, list[str]] = {}
    for item in headers or []:
        name = (item.get("name") or "").strip().lower()
        if name:
            out.setdefault(name, []).append(item.get("value") or "")
    return out


def first(hmap, name):
    values = hmap.get(name.lower())
    return values[0] if values else None


def parse_list_unsubscribe(value) -> list[str]:
    """RFC 2369: one or more <uri> entries, possibly folded over several lines."""
    if not value or len(value) > MAX_HEADER:
        return []
    return [re.sub(r"\s+", "", raw) for raw in _ANGLE.findall(value) if raw.strip()]


def split_unsubscribe_uris(uris):
    https, http, mailto = [], [], []
    for uri in uris:
        low = uri.lower()
        if low.startswith("https://"):
            https.append(uri)
        elif low.startswith("http://"):
            http.append(uri)
        elif low.startswith("mailto:"):
            mailto.append(uri)
    return https, http, mailto


def has_one_click(value) -> bool:
    """RFC 8058: List-Unsubscribe-Post: List-Unsubscribe=One-Click"""
    return bool(value) and "list-unsubscribe=one-click" in re.sub(r"\s+", "", value).lower()


def parse_auth_results(values) -> dict[str, str]:
    """Verdicts from the topmost Authentication-Results header that has any.

    The receiving server (Microsoft) adds its header at the top; anything lower
    down could have been forged by the sender, so it is ignored.
    """
    for value in values or []:
        verdicts: dict[str, str] = {}
        for key, verdict in _AUTH.findall(value):
            verdicts.setdefault(key.lower(), verdict.lower())
        if verdicts:
            return verdicts
    return {}


def parse_mailto(uri):
    """mailto:addr?subject=..&body=.. -> dict, or None if it isn't a single sane address."""
    parts = urlsplit(uri)
    if parts.scheme.lower() != "mailto":
        return None
    address = unquote(parts.path).strip()
    if not _EMAIL.fullmatch(address):
        return None
    # The sender's own subject/body are deliberately ignored: otherwise a
    # malicious header could make you send an email with text of its choosing.
    return {"address": address, "subject": "unsubscribe", "body": "unsubscribe"}


# What a real unsubscribe mailbox is called. Two groups, because a plain
# substring test let "christopher" through on "stop" and "cleaver" on
# "leave" — handing a spammer the very reply this check exists to withhold.
#
# Distinctive enough to match as the start of a word ("unsubscribe",
# "newsletter-remove"):
_UNSUB_PREFIX = ("unsub", "optout", "opt[-_]out", "remove", "delist", "listserv",
                 "majordomo", "mailing", "newsletter", "subscri", "preferences")
# Short and liable to hide inside a surname, so these must be a whole word:
# "cancel" yes, "cancellara" no; "bounces" yes, "bouncer" no.
_UNSUB_WORD = ("leave", "cancel", "bounces?", "request", "abuse")
# "stop" and "quit" are deliberately absent: SMS conventions, rare in email,
# and the likeliest of all to be part of someone's name.
_UNSUB_RE = re.compile(
    r"(?:^|[^a-z])(?:(?:" + "|".join(_UNSUB_PREFIX) + r")"
    r"|(?:(?:" + "|".join(_UNSUB_WORD) + r")(?:[^a-z]|$)))",
    re.IGNORECASE)
# An opaque per-recipient token, e.g. unsubscribe-mc.us4_6bceaaa7f58@... The
# length and the digits are what make it a token rather than someone's name.
_TOKENISH = re.compile(r"[A-Za-z0-9_.+-]{16,}")


def looks_like_unsubscribe_mailbox(address: str) -> bool:
    """Does this address look like a list's unsubscribe system, rather than a
    person's mailbox? Used to decide whether MailSift may email it."""
    local = (address or "").rsplit("@", 1)[0].lower()
    if not local:
        return False
    if _UNSUB_RE.search(local):
        return True
    return bool(_TOKENISH.fullmatch(local) and any(c.isdigit() for c in local))


def normalise_address(value) -> str:
    """The plain address out of whatever Graph hands back.

    Graph passes on what was in the From header, and a sender can malform
    that. One arrived as "<w3a9h5h2w3@...>", angle brackets and all, which
    Outlook then refused to put in an inbox rule — failing every block in
    that clean-up, not just the one. Cleaned at the door instead, so an odd
    sender can't poison a rule, a comparison or the display.

    Returns "" for anything with no usable address in it.
    """
    text = (value or "").strip()
    if len(text) > MAX_HEADER:
        return ""
    match = _EMAIL.search(text)
    return match.group(0).lower() if match else ""


def emails_in(text) -> list[str]:
    return [m.lower() for m in _EMAIL.findall(text or "")]
