"""MailSift must not email spammers.

Built from a real incident: a clean-up on a live mailbox sent "unsubscribe"
from the user's own account to vonni.n@cdfengyun.com, madlin-grant@geyangled.com,
aclaiborne@aliketrap.megabet-ph.net and others, then filled the inbox with
bounces. Every one of those senders passed SPF, DKIM and DMARC, because the
spammer owned the domain, so the only gate MailSift had let them straight
through. Replying to spam is the one thing the whole classifier exists to
prevent: it confirms a human reads the address.
"""
import pytest

from mailsift.actions import Actions
from mailsift.demo import DemoGraph
from mailsift.headers import looks_like_unsubscribe_mailbox
from mailsift.score import assess, unsub_by_email
from mailsift.store import Store
from tests.test_score import NOW

# Addresses MailSift actually emailed, from the Sent Items screenshot.
REAL_INCIDENT = [
    "vonni.n@cdfengyun.com",
    "tv725@cdfengyun.com",
    "madlin-grant@geyangled.com",
    "aclaiborne@aliketrap.megabet-ph.net",
    "natalee-mathias@hkfurcity.com",
]
# Addresses from genuine unsubscribes in the same mailbox, which must still work.
REAL_LEGITIMATE = [
    "unsubscribe+20500653+27142605@unsubscribe.iterable.com",
    "unsubscribe-mc.us4_6bceaaa7f5856768f4be23c55.2048a49016-997beaab74@unsubscribe.mailchimpapp.net",
    "leave@fitfuel.co.uk",
    "unsub@voltbox.co.uk",
    "optout@brand.example",
    "newsletter-remove@smallbiz.co.uk",
]


def sender(**kw):
    s = {"address": "news@example.com", "name": "Example", "kind": "mailto", "ids": [], "folders": [],
         "https": [], "http": [], "mailto": [], "read": 0, "count": 40, "junk": 0,
         "emailed": False, "protected": False, "blocked": False, "ignored": False, "ignored_count": 0,
         "per_month": 10.0, "recent_count": 12, "recent_opened": 0, "older_count": 28, "older_opened": 0}
    s.update(kw)
    return s


class Mailbox(DemoGraph):
    """Records anything MailSift tries to send."""

    def __init__(self):
        super().__init__(delay=0)
        self.sent = []


def apply_one(s):
    graph = Mailbox()
    actions = Actions(graph, Store(":memory:"), poster=lambda url: (False, "no one-click"))
    result = actions._unsubscribe(s)
    return graph.sent, result


# -- the incident itself ---------------------------------------------------------
@pytest.mark.parametrize("address", REAL_INCIDENT)
def test_the_addresses_it_actually_emailed_are_now_refused(address):
    sent, result = apply_one(sender(mailto=[f"mailto:{address}"]))
    assert sent == [], f"emailed {address} again"
    assert result["status"] == "failed"
    assert "Block them instead" in result["outcome"]
    assert address in result["outcome"]


@pytest.mark.parametrize("address", REAL_LEGITIMATE)
def test_genuine_unsubscribe_addresses_still_work(address):
    sent, result = apply_one(sender(mailto=[f"mailto:{address}"]))
    assert [m["to"] for m in sent] == [address]
    assert result["status"] == "done"
    assert sent[0]["subject"] == "unsubscribe" and sent[0]["body"] == "unsubscribe"


# -- what earns the right to be emailed --------------------------------------------
def test_a_sender_you_have_opened_is_still_emailed():
    sent, _ = apply_one(sender(read=3, mailto=["mailto:vonni.n@cdfengyun.com"]))
    assert len(sent) == 1


def test_a_sender_you_have_written_to_is_still_emailed():
    sent, _ = apply_one(sender(emailed=True, mailto=["mailto:anything@wherever.example"]))
    assert len(sent) == 1


def test_an_email_platform_domain_is_trusted():
    sent, _ = apply_one(sender(mailto=["mailto:x9@mailchimpapp.net"]))
    assert len(sent) == 1


def test_a_usable_address_is_preferred_over_a_refused_one():
    sent, result = apply_one(sender(mailto=["mailto:vonni.n@cdfengyun.com", "mailto:unsub@cdfengyun.com"]))
    assert [m["to"] for m in sent] == ["unsub@cdfengyun.com"] and result["status"] == "done"


def test_refusing_to_email_still_offers_their_web_page():
    sent, result = apply_one(sender(mailto=["mailto:vonni.n@cdfengyun.com"],
                                    https=["https://cdfengyun.com/preferences"]))
    assert sent == []
    assert result["status"] == "needs_you" and result["link"] == "https://cdfengyun.com/preferences"


def test_a_refused_sender_is_not_recorded_as_unsubscribed():
    graph = Mailbox()
    store = Store(":memory:")
    Actions(graph, store, poster=lambda url: (False, "x"))._unsubscribe(
        sender(address="spam@cdfengyun.com", mailto=["mailto:vonni.n@cdfengyun.com"]))
    assert store.unsubscribed() == {}


def test_dry_run_never_sends_even_when_allowed():
    graph = Mailbox()
    result = Actions(graph, Store(":memory:"), dry_run=True)._unsubscribe(
        sender(mailto=["mailto:unsub@brand.example"]))
    assert graph.sent == [] and result["status"] == "dry_run"


# -- address shapes ----------------------------------------------------------------
@pytest.mark.parametrize("address", REAL_INCIDENT + ["bob.smith@firm.com", "j.doe@x.io", "sales@x.io"])
def test_personal_mailboxes_are_not_unsubscribe_addresses(address):
    assert looks_like_unsubscribe_mailbox(address) is False


@pytest.mark.parametrize("address", REAL_LEGITIMATE)
def test_unsubscribe_mailboxes_are_recognised(address):
    assert looks_like_unsubscribe_mailbox(address) is True


@pytest.mark.parametrize("bad", ["", None, "@", "no-at-sign", "@domain.com"])
def test_odd_addresses_dont_crash(bad):
    assert looks_like_unsubscribe_mailbox(bad) is False
    assert Actions.may_email_unsubscribe(sender(), bad) is False


# -- smart pick must not suggest what apply would refuse ----------------------------
def test_smart_pick_suggests_blocking_these_rather_than_emailing_them():
    s = sender(mailto=["mailto:vonni.n@cdfengyun.com"], per_month=12.0, count=60, recent_count=20)
    verdict = assess(s, NOW)
    assert verdict["tier"] == "pick"
    assert verdict["suggest"] == "block", "would have told apply to email a spammer"


def test_smart_pick_still_suggests_unsubscribing_from_a_real_list():
    s = sender(mailto=["mailto:leave@fitfuel.co.uk"], per_month=12.0, count=60, recent_count=20)
    assert assess(s, NOW)["suggest"] == "unsubdel"


def test_the_score_and_apply_agree_on_every_shape():
    # If these two ever disagree, smart pick selects senders that then fail.
    for address in REAL_INCIDENT + REAL_LEGITIMATE:
        for read, emailed in ((0, False), (4, False), (0, True)):
            s = sender(mailto=[f"mailto:{address}"], read=read, emailed=emailed)
            assert unsub_by_email(s) == Actions.may_email_unsubscribe(s, address), (address, read, emailed)


# -- the guard must not be fooled by a word hiding inside a name -------------------
@pytest.mark.parametrize("address", [
    "christopher@spammer.com",      # ...chri-STOP-her
    "cleaver@spam.net",             # ...c-LEAVE-r
    "chris.stopford@x.com",
    "steve.quitman@x.com",
    "aremover@x.com",
    "ms.cancellara@x.com",
    "joe.bouncer@x.com",   # whole-word matching, not a prefix
])
def test_a_person_whose_name_contains_an_unsubscribe_word_is_still_refused(address):
    assert looks_like_unsubscribe_mailbox(address) is False
    sent, _ = apply_one(sender(mailto=[f"mailto:{address}"]))
    assert sent == [], f"emailed {address}"


@pytest.mark.parametrize("address", [
    "unsubscribe@brand.com", "unsub@brand.com", "newsletter-unsubscribe@brand.com",
    "list-leave@brand.com", "leave@brand.com", "remove@brand.com", "opt-out@brand.com",
    "manage-subscription@brand.com", "email-preferences@brand.com", "abuse@brand.com",
    "mylist-request@brand.com", "bounces+3@brand.com",
])
def test_real_unsubscribe_mailboxes_are_still_recognised(address):
    assert looks_like_unsubscribe_mailbox(address) is True
