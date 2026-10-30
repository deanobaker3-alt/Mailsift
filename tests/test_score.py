"""The junk score. Property tests over thousands of generated senders, because
the guarantees that matter (never unsafe, never backwards) have to hold for
every mailbox, not just the examples someone thought of."""
import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from mailsift.classify import ALLOWED_ACTIONS
from mailsift.score import MIN_EVIDENCE, PICK, assess, trend_of

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
KINDS = list(ALLOWED_ACTIONS)


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sender(count=50, recent=12, recent_opened=0, older_opened=0, per_month=4.0, junk=0,
           kind="oneclick", **extra):
    older = count - recent
    s = {
        "kind": kind, "count": count, "read": recent_opened + older_opened, "per_month": per_month,
        "junk": junk, "recent_count": recent, "recent_opened": recent_opened,
        "older_count": older, "older_opened": older_opened,
        # The newest opened email: a recent one if any recent was opened.
        "last_opened": iso(10) if recent_opened else iso(200) if older_opened else None,
        "protected": False, "blocked": False, "emailed": False, "ignored": False, "ignored_count": 0,
    }
    s.update(extra)
    return s


def random_sender(rng):
    count = rng.randint(1, 400)
    recent = rng.randint(0, count)
    recent_opened = rng.randint(0, recent)
    older_opened = rng.randint(0, count - recent)
    return sender(count=count, recent=recent, recent_opened=recent_opened, older_opened=older_opened,
                  per_month=round(rng.uniform(0, 40), 1), junk=rng.randint(0, count),
                  kind=rng.choice(KINDS))


# -- safety: holds for every input ---------------------------------------------
def test_never_recommends_an_action_the_sender_kind_forbids():
    rng = random.Random(1)
    for _ in range(5000):
        s = random_sender(rng)
        s.update(protected=rng.random() < 0.05, blocked=rng.random() < 0.05,
                 emailed=rng.choice([True, False, None]), ignored=rng.random() < 0.05)
        r = assess(s, NOW)
        assert r["suggest"] is None or r["suggest"] in ALLOWED_ACTIONS[s["kind"]], (s, r)
        assert 0 <= r["score"] <= 100
        assert r["tier"] in {"pick", "suggest", "leave"}
        assert (r["tier"] == "leave") == (r["suggest"] is None), r
        assert all(isinstance(w, str) and w for w in r["score_why"])


def test_suspicious_senders_are_never_recommended_for_unsubscribing():
    for kind in ("dodgy", "unverified"):
        for opened in (0, 5):
            r = assess(sender(kind=kind, recent_opened=0, older_opened=opened, per_month=30), NOW)
            assert r["suggest"] in (None, "block")


def test_only_well_understood_senders_are_ever_auto_picked():
    # Unverified senders and lists with no unsubscribe option are, at most,
    # suggested: blocking a real sender is too big a step to take silently.
    for kind in ("unverified", "none"):
        r = assess(sender(kind=kind, per_month=40, count=300, recent=60), NOW)
        assert r["tier"] != "pick"


@pytest.mark.parametrize("flag", ["protected", "blocked", "emailed"])
def test_hard_overrides_win_over_any_numbers(flag):
    worst = sender(count=400, recent=100, per_month=40, junk=400, **{flag: True})
    r = assess(worst, NOW)
    assert r["tier"] == "leave" and r["suggest"] is None


def test_emailed_unknown_is_not_treated_as_emailed():
    # None means Sent Items couldn't be checked: score normally.
    r = assess(sender(per_month=30, count=200, recent=50, emailed=None), NOW)
    assert r["tier"] == "pick"


def test_ignored_unsubscribe_means_block():
    r = assess(sender(ignored=True, ignored_count=6, older_opened=40), NOW)
    assert r["tier"] == "pick" and r["suggest"] == "block"
    assert "6" in r["score_why"][0]


# -- never backwards -------------------------------------------------------------
def test_opening_more_never_raises_the_score():
    rng = random.Random(2)
    checked = 0
    for _ in range(4000):
        s = random_sender(rng)
        before = assess(s, NOW)["score"]
        if s["recent_opened"] < s["recent_count"]:
            more = sender(**{**_args(s), "recent_opened": s["recent_opened"] + 1})
            assert assess(more, NOW)["score"] <= before, (s, more)
            checked += 1
        if s["older_opened"] < s["older_count"]:
            more = sender(**{**_args(s), "older_opened": s["older_opened"] + 1})
            assert assess(more, NOW)["score"] <= before, (s, more)
            checked += 1
    assert checked > 3000


def test_more_mail_never_lowers_the_score():
    rng = random.Random(3)
    for _ in range(4000):
        s = random_sender(rng)
        before = assess(s, NOW)["score"]
        busier = sender(**{**_args(s), "per_month": s["per_month"] + rng.uniform(0.1, 10)})
        assert assess(busier, NOW)["score"] >= before
        # One more unopened email. It's counted as junk if they already have
        # any, since adding a clean one rightly lowers the junk share.
        bigger = sender(**{**_args(s), "count": s["count"] + 1, "junk": s["junk"] + (1 if s["junk"] else 0)})
        assert assess(bigger, NOW)["score"] >= before


def _args(s):
    return {"count": s["count"], "recent": s["recent_count"], "recent_opened": s["recent_opened"],
            "older_opened": s["older_opened"], "per_month": s["per_month"], "junk": s["junk"],
            "kind": s["kind"]}


def test_fading_away_never_outscores_never_opening():
    never = assess(sender(count=60, recent=16, recent_opened=0, older_opened=0), NOW)["score"]
    faded = assess(sender(count=60, recent=16, recent_opened=0, older_opened=20), NOW)["score"]
    assert faded <= never


# -- cautious --------------------------------------------------------------------
def test_thin_evidence_is_never_auto_picked():
    for count in range(1, MIN_EVIDENCE):
        r = assess(sender(count=count, recent=count, per_month=40, junk=count), NOW)
        assert r["tier"] != "pick", (count, r)
        assert any("not much to go on" in w for w in r["score_why"])


# -- the trend -------------------------------------------------------------------
def test_faded_is_detected_and_explained():
    s = sender(count=70, recent=16, recent_opened=0, older_opened=22)
    assert trend_of(s) == "faded"
    r = assess(s, NOW)
    assert any("used to open" in w and "none of the last 16" in w for w in r["score_why"])


def test_rising_is_detected_and_lowers_rather_than_raises():
    s = sender(count=60, recent=12, recent_opened=8, older_opened=2)
    assert trend_of(s) == "rising"
    assert any("more lately" in w for w in assess(s, NOW)["score_why"])


def test_no_trend_on_too_little_data():
    assert trend_of(sender(count=6, recent=2, recent_opened=0, older_opened=4)) is None


# -- robust to anything ----------------------------------------------------------
@pytest.mark.parametrize("junk", [
    {}, {"count": None}, {"count": "lots"}, {"count": -5, "read": 10}, {"count": math.nan},
    {"count": 10, "read": 999, "per_month": math.inf}, {"kind": "made-up", "count": 50},
    {"count": 20, "last_opened": "not a date"}, {"count": 20, "recent_count": 50, "recent_opened": 60},
])
def test_odd_input_never_raises(junk):
    r = assess(junk, NOW)
    assert 0 <= r["score"] <= 100 and r["tier"] in {"pick", "suggest", "leave"}


def test_deterministic():
    rng = random.Random(4)
    for _ in range(200):
        s = random_sender(rng)
        assert assess(s, NOW) == assess(dict(s), NOW)


def test_clear_junk_is_picked_and_a_loved_newsletter_is_left():
    assert assess(sender(count=200, recent=50, per_month=17), NOW)["tier"] == "pick"
    assert assess(sender(count=50, recent=12, recent_opened=9, older_opened=25, per_month=4), NOW)["tier"] == "leave"
    assert PICK > 50
