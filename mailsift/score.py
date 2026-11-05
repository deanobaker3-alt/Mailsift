"""The junk score behind smart pick: 0 (clearly wanted) to 100 (clearly not).

One pure function, no I/O, so it can be tested exhaustively. Design rules:

* Explainable. Every point comes with a plain-English reason, shown next to
  the sender, because an unsubscribe can't be undone and nobody should have to
  trust a number they can't see the workings of.
* Cautious. A wrong unsubscribe costs more than a missed one, so thin
  evidence pulls the score down, and anything you've written to yourself is
  never picked however rarely you open it.
* Safe by construction. It only ever recommends an action the sender's kind
  allows (classify.ALLOWED_ACTIONS), so a suspicious sender can never be
  recommended for unsubscribing, whatever the numbers say. Senders whose only
  unsubscribe route is emailing them are recommended for blocking instead
  when there's nothing to suggest they're a real list (see unsub_by_email).
* Monotonic. Opening more of a sender's mail never raises its score; more
  mail from them never lowers it. Tested.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

from .classify import ALLOWED_ACTIONS, registrable_domain
from .company import PLATFORMS
from .headers import looks_like_unsubscribe_mailbox, parse_mailto

RECENT_WINDOW = timedelta(days=90)   # "lately", for the engagement trend

PICK = 70       # smart pick selects these
SUGGEST = 45    # shown as worth a look, never selected automatically

# Weights. Maximum contributions; the total is clamped to 0..100.
W_OPENS = 55         # never opening them
W_VOLUME = 25        # how much they send (full marks at VOLUME_FULL a month)
W_JUNK = 15          # how much of it Outlook already files as junk
W_NEVER = 10         # not one opened, out of a decent number
W_QUIET = 15         # none of the recent ones opened (covers "faded" too)
W_STALE = 5          # the newest email you opened is half a year old
# The trend has no points of its own on purpose. "Faded" scoring above
# "never opened" would be backwards, and a "rising" credit would let opening
# more old mail *raise* a score. Both effects come through the recency-weighted
# open rate and W_QUIET instead, which keeps the score monotonic.

OPEN_FULL = 0.30     # opening 30%+ of their mail counts as fully engaged
VOLUME_FULL = 8.0    # 8+ emails a month counts as fully noisy
MIN_EVIDENCE = 5     # fewer emails than this and the score is scaled down
TREND_MIN = 4        # emails needed in each window before trusting a trend
STALE_DAYS = 180

FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _num(value, default=0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    # rejects NaN, infinities and negatives
    return number if math.isfinite(number) and number >= 0 else default


def trend_of(s) -> str | None:
    """'faded', 'rising', or None when steady or there isn't enough to tell."""
    recent, recent_opened = _num(s.get("recent_count")), _num(s.get("recent_opened"))
    older, older_opened = _num(s.get("older_count")), _num(s.get("older_opened"))
    if recent < TREND_MIN or older < TREND_MIN + 1:
        return None
    recent_rate = min(recent_opened / recent, 1.0)
    older_rate = min(older_opened / older, 1.0)
    if older_rate >= 0.10 and recent_rate <= older_rate * 0.25:
        return "faded"
    if recent_rate >= 0.20 and recent_rate >= older_rate + 0.15:
        return "rising"
    return None


def unsub_by_email(s) -> bool:
    """Is emailing this sender's unsubscribe address a reasonable thing to
    suggest? Mirrors Actions.may_email_unsubscribe, which enforces it for
    real; this keeps smart pick from offering what apply would refuse."""
    if s.get("emailed") or s.get("read"):
        return True
    for uri in s.get("mailto") or ():
        mail = parse_mailto(uri)
        if mail and (looks_like_unsubscribe_mailbox(mail["address"])
                     or registrable_domain(mail["address"].rsplit("@", 1)[-1]) in PLATFORMS):
            return True
    return not (s.get("mailto") or ())   # nothing to judge: leave as it was


def _result(score, tier, action, why, trend):
    return {"score": int(round(max(0.0, min(100.0, score)))), "tier": tier,
            "suggest": action, "score_why": why, "trend": trend}


def _best_action(kind: str, prefer: str, sender=None) -> str | None:
    allowed = ALLOWED_ACTIONS.get(kind, {"keep"})
    # Unsubscribing from a mailto-only sender means emailing them. Don't put
    # that in front of someone when the sender shows no sign of being a real
    # mailing list: blocking gets the same quiet inbox without telling a
    # probable spammer that the address is live.
    if prefer in ("unsub", "unsubdel") and sender is not None and kind == "mailto" \
            and not unsub_by_email(sender):
        prefer = "block"
    if prefer in allowed:
        return prefer
    return "block" if "block" in allowed else None


def assess(s: dict, now: datetime) -> dict:
    """Scores one sender (a scan result dict). Never raises on odd input."""
    kind = s.get("kind") or "none"
    count = int(_num(s.get("count")))
    opened = min(int(_num(s.get("read"))), count)
    per_month = _num(s.get("per_month"))
    junk = min(int(_num(s.get("junk"))), count)
    trend = trend_of(s)

    # -- absolute rules first: these aren't a matter of degree --------------
    if s.get("protected"):
        return _result(0, "leave", None, ["on your never-touch list"], trend)
    if s.get("blocked"):
        return _result(0, "leave", None, ["already blocked"], trend)
    if s.get("emailed"):
        return _result(5, "leave", None, ["you've emailed this address yourself"], trend)
    if s.get("ignored"):
        return _result(97, "pick", _best_action(kind, "block", s),
                       [f"kept emailing after you unsubscribed ({int(_num(s.get('ignored_count')))} since)"], trend)
    if kind == "dodgy":
        return _result(95, "pick", _best_action(kind, "block", s), ["looks like spam or a scam: block, never unsubscribe"], trend)
    if count == 0:
        return _result(0, "leave", None, [], trend)

    # -- the weighted part ----------------------------------------------------
    why, score = [], 0.0
    rate = opened / count
    recent = _num(s.get("recent_count"))
    recent_rate = min(_num(s.get("recent_opened")) / recent, 1.0) if recent >= TREND_MIN else None
    # What you do lately matters more than what you did a year ago.
    effective = rate if recent_rate is None else 0.7 * recent_rate + 0.3 * rate

    score += W_OPENS * (1 - min(effective / OPEN_FULL, 1.0))
    if opened == 0:
        why.append(f"never opened any of {count:,}")
    else:
        why.append(f"opened {opened:,} of {count:,}")

    score += W_VOLUME * min(per_month / VOLUME_FULL, 1.0)
    if per_month >= 1:
        why.append(f"about {round(per_month):,} a month")

    if junk:
        score += W_JUNK * (junk / count)
        why.append(f"Outlook filed {round(100 * junk / count)}% as junk")

    if opened == 0 and count >= MIN_EVIDENCE:
        score += W_NEVER

    recent_opened = int(_num(s.get("recent_opened")))
    quiet = recent_rate is not None and recent_opened == 0
    if quiet:
        score += W_QUIET

    if trend == "faded":
        older = _num(s.get("older_count"))
        was = round(100 * _num(s.get("older_opened")) / older) if older else 0
        lately = "none" if recent_opened == 0 else f"only {recent_opened}"
        why.append(f"you used to open about {was}%, but {lately} of the last {int(recent)}")
    elif trend == "rising":
        why.append("you've been opening these more lately")
    elif quiet and opened:
        why.append(f"none of the last {int(recent)} opened")

    last_opened = _parse(s.get("last_opened"))
    if last_opened is not None and not quiet:
        days = (now - last_opened).days
        if days >= STALE_DAYS:
            score += W_STALE
            why.append(f"the newest one you opened arrived {days // 30} months ago")

    score = max(0.0, min(100.0, score))   # clamp before scaling, or 110 * 0.8 still reads as 88
    thin = count < MIN_EVIDENCE
    if thin:
        score *= count / MIN_EVIDENCE
        why.append(f"only {count} so far, not much to go on")

    # -- tiers and the action that goes with them ----------------------------
    if kind == "unverified":
        # Can't tell who sent it: never auto-picked, and blocking is the only
        # thing on offer.
        why.append("couldn't verify who sent these, so only blocking is offered")
        tier = "suggest" if score >= SUGGEST else "leave"
        return _result(score, tier, _best_action(kind, "block", s) if tier != "leave" else None, why, trend)

    if kind == "none":
        # A genuine list with no unsubscribe option: blocking a real sender is
        # a bigger step than unsubscribing, so it's only ever suggested.
        tier = "suggest" if score >= SUGGEST else "leave"
        return _result(score, tier, _best_action(kind, "block", s) if tier != "leave" else None, why, trend)

    tier = "pick" if score >= PICK else "suggest" if score >= SUGGEST else "leave"
    if thin and tier == "pick":
        tier = "suggest"   # a hard rule, not left to the arithmetic
    action = _best_action(kind, "unsubdel", s) if tier != "leave" else None
    return _result(score, tier, action, why, trend)
