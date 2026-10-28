"""Decides, per sender, how it can be unsubscribed from, or whether it looks
dodgy and should only ever be blocked.

The golden rule: never unsubscribe from spam. Clicking "unsubscribe" on a
spammer's email just confirms the address is live, and the link may be the
phish. So anything that fails authentication or looks like impersonation is
"block only".
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

from .headers import emails_in

SUSPICIOUS_TLDS = {
    "top", "xyz", "click", "buzz", "rest", "cam", "icu", "monster", "cfd", "sbs", "lol", "quest",
    "bond", "zip", "mov", "gq", "tk", "ml", "cf", "ga", "work", "fit", "country", "kim", "men",
    "loan", "win", "bid", "stream", "review", "date", "racing", "download", "party", "trade",
    "accountant", "science", "cricket", "faith", "shop", "online", "site", "live", "beauty",
}

MULTI_PART_SUFFIXES = {
    "co.uk", "org.uk", "gov.uk", "ac.uk", "ltd.uk", "plc.uk", "me.uk", "net.uk", "sch.uk", "nhs.uk",
    "com.au", "net.au", "org.au", "co.nz", "co.za", "com.br", "co.jp", "co.in", "com.mx", "com.sg",
}

# Names scammers love to borrow. Only names of 4+ letters, to avoid false hits,
# and only strings unlikely to turn up inside an unrelated domain: "chase" would
# match "purchase", "boots" would match "bootstrap".
BRANDS = {
    "amazon", "paypal", "apple", "microsoft", "netflix", "royalmail", "hmrc", "dvla", "evri",
    "hermes", "dhl", "fedex", "barclays", "hsbc", "lloyds", "natwest", "santander", "halifax",
    "nationwide", "monzo", "revolut", "ebay", "facebook", "instagram", "google", "outlook",
    "hotmail", "office365", "docusign", "coinbase", "binance", "argos", "tesco", "sainsburys",
    "virginmedia", "vodafone", "disney", "spotify", "whatsapp", "linkedin", "dropbox",
    # UK public bodies, utilities, couriers and retailers that get impersonated a lot
    "tvlicensing", "companieshouse", "parcelforce", "postoffice", "yodel", "ofgem", "ofcom",
    "britishgas", "octopusenergy", "ovoenergy", "eonnext", "edfenergy", "scottishpower",
    # (Severn Trent deliberately left out: it sends from stwater.co.uk, which
    # shares nothing with the name, so it would flag its own real mail.)
    "thameswater", "talktalk", "giffgaff", "plusnet", "marksandspencer",
    "johnlewis", "waitrose", "morrisons", "screwfix", "currys", "ticketmaster", "trainline",
    "nationalrail", "easyjet", "ryanair", "britishairways", "deliveroo", "justeat", "airbnb",
    "mastercard", "americanexpress", "wetransfer", "malwarebytes", "mcafee", "adobe",
    "starling", "virginmoney", "firstdirect", "metrobank",
}

# Brand names that are also everyday words ("The Weekly Outlook"), so they're
# not treated as impersonation when they appear in a display name.
EVERYDAY_WORDS = {"outlook", "apple", "hermes", "disney"}

_HOMOGLYPHS =[("rn", "m"), ("vv", "w"), ("0", "o"), ("1", "l"), ("3", "e"), ("4", "a"), ("5", "s"), ("7", "t")]

# A word of 4+ letters with a short alphanumeric tail glued straight onto it,
# e.g. "TV LICENSINGoz6Fc". Spammers add these so no two copies of the email
# look alike to a filter. The tail still has to mix digits and both cases
# before we call it random, so "Formula1", "iPhone15" and "Log4j" don't count.
_GLUED_TAIL = re.compile(r"(?:[A-Z]{4,}|[A-Z][a-z]{3,})([A-Za-z0-9]{4,12})(?![A-Za-z0-9])")


def random_looking_suffix(display_name: str) -> str:
    """The random-looking tail in a display name, or "" if there isn't one."""
    for tail in _GLUED_TAIL.findall(display_name or ""):
        if (any(c.isdigit() for c in tail)
                and any(c.islower() for c in tail)
                and any(c.isupper() for c in tail)):
            return tail
    return ""

# Which actions the UI may offer for each kind of sender.
ALLOWED_ACTIONS = {
    "oneclick": {"keep", "unsub", "unsubdel", "block"},
    "mailto": {"keep", "unsub", "unsubdel", "block"},
    "link": {"keep", "unsub", "unsubdel", "block"},
    "none": {"keep", "block"},
    "unverified": {"keep", "block"},
    "dodgy": {"keep", "block"},
}

AUTH_PASS = {"pass", "bestguesspass"}

_PRETTY = {
    "paypal": "PayPal", "hmrc": "HMRC", "dvla": "DVLA", "dhl": "DHL", "hsbc": "HSBC", "natwest": "NatWest",
    "royalmail": "Royal Mail", "ebay": "eBay", "fedex": "FedEx", "linkedin": "LinkedIn", "whatsapp": "WhatsApp",
    "virginmedia": "Virgin Media", "office365": "Office 365", "docusign": "DocuSign", "sainsburys": "Sainsbury's",
    "tvlicensing": "TV Licensing", "companieshouse": "Companies House", "postoffice": "Post Office",
    "britishgas": "British Gas", "octopusenergy": "Octopus Energy", "ovoenergy": "OVO Energy",
    "eonnext": "E.ON Next", "edfenergy": "EDF Energy", "scottishpower": "ScottishPower",
    "thameswater": "Thames Water", "severntrent": "Severn Trent", "talktalk": "TalkTalk",
    "giffgaff": "giffgaff", "marksandspencer": "Marks & Spencer", "johnlewis": "John Lewis",
    "nationalrail": "National Rail", "britishairways": "British Airways", "justeat": "Just Eat",
    "americanexpress": "American Express", "wetransfer": "WeTransfer", "mcafee": "McAfee",
    "easyjet": "easyJet", "firstdirect": "first direct", "metrobank": "Metro Bank",
    "virginmoney": "Virgin Money",
}


def pretty(brand: str) -> str:
    return _PRETTY.get(brand, brand.title())


def registrable_domain(domain: str) -> str:
    labels = [l for l in domain.lower().strip(".").split(".") if l]
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _variants(label: str):
    normal = label
    for a, b in _HOMOGLYPHS:
        normal = normal.replace(a, b)
    return {normal, normal.replace("l", "i"), label.replace("l", "i"), normal.replace("i", "l")}


def lookalike_brand(domain: str):
    """('amazon', 'homoglyph') for amaz0n-deals.top, ('amazon', 'name') for
    amazon-prizes.com, None for amazon.co.uk or unrelated domains."""
    if not domain:
        return None
    label = registrable_domain(domain).split(".")[0]
    for brand in sorted(BRANDS):
        if label == brand:
            return None
        if brand in label:
            return brand, "name"
    for brand in sorted(BRANDS):
        if any(brand in variant for variant in _variants(label)):
            return brand, "homoglyph"
    return None


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _is_ip_host(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def classify(*, address, display_name, auth, one_click, https, http, mailto, junk_count, count):
    reasons: list[str] = []
    domain = address.rsplit("@", 1)[-1].lower() if "@" in address else ""
    reg = registrable_domain(domain) if domain else ""
    label = reg.split(".")[0] if reg else ""
    tld = reg.rsplit(".", 1)[-1] if "." in reg else ""

    dmarc, dkim, compauth = auth.get("dmarc"), auth.get("dkim"), auth.get("compauth")
    authed = dmarc in AUTH_PASS or dkim == "pass" or compauth == "pass"

    if dmarc in ("fail", "permerror") or compauth == "fail":
        reasons.append("Failed sender authentication (DMARC)")
    if tld in SUSPICIOUS_TLDS:
        reasons.append(f"Sent from a .{tld} domain, a favourite with spammers")

    look = lookalike_brand(domain)
    if look and look[1] == "homoglyph":
        reasons.append(f"Domain is a lookalike of {pretty(look[0])}")
    elif look and look[1] == "name" and (tld in SUSPICIOUS_TLDS or not authed):
        reasons.append(f"Uses {pretty(look[0])}'s name on a domain that isn't theirs")

    # Compared against the whole registrable domain with punctuation removed,
    # not just its first label: plenty of real senders split their name across
    # the domain and the TLD (octopus.energy, plus.net, just-eat.co.uk) and
    # must not be accused of impersonating themselves.
    squashed_name = _squash(display_name)
    squashed_domain = _squash(reg)
    for brand in sorted(BRANDS - EVERYDAY_WORDS):
        if brand in squashed_name and brand not in squashed_domain:
            if not (look and look[0] == brand):
                reasons.append(f"Calls itself {pretty(brand)} but doesn't send from {pretty(brand)}")
            break

    tail = random_looking_suffix(display_name)
    if tail:
        reasons.append(f"Sender name ends in random characters (“{tail}”), a spam trick")

    if any(e != address.lower() for e in emails_in(display_name)):
        reasons.append("Display name shows a different email address")
    if any(_is_ip_host(u) for u in https + http):
        reasons.append("Unsubscribe link points at a raw IP address")
    if count and junk_count / count >= 0.5 and not authed:
        reasons.append("Outlook already files most of these as junk")

    if reasons:
        kind = "dodgy"
    elif not auth or not authed:
        kind, reasons = "unverified", ["Couldn't verify who really sent these"]
    elif one_click and https:
        kind = "oneclick"
    elif mailto:
        kind = "mailto"
    elif https or http:
        kind = "link"
    else:
        kind = "none"
    return {"kind": kind, "reasons": reasons}
