"""Parsing for the email headers MailSift relies on."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

_ANGLE = re.compile(r"<([^>]*)>")
_AUTH = re.compile(r"\b(spf|dkim|dmarc|compauth)\s*=\s*([a-zA-Z]+)", re.IGNORECASE)
_EMAIL = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")


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
    if not value:
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


def emails_in(text) -> list[str]:
    return [m.lower() for m in _EMAIL.findall(text or "")]
