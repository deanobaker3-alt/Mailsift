"""The README and SECURITY.md both promise that a suspicious sender can only
be blocked, never unsubscribed, and that this is "enforced server-side, not
just hidden in the UI". That promise is the one most likely to cost a user
something real if it ever stopped being true: clicking unsubscribe on a scam
confirms to the sender that the address is live.

So these tests attack it the way an attacker would, bypassing the UI and
posting the forbidden action straight at Actions.apply.
"""
import pytest

from mailsift.actions import Actions
from mailsift.classify import ALLOWED_ACTIONS
from mailsift.demo import DemoGraph, seed_demo_store
from mailsift.scanner import Scanner
from mailsift.store import Store

RISKY = ("dodgy", "unverified", "none")


@pytest.fixture
def env():
    graph, store = DemoGraph(delay=0), Store(":memory:")
    seed_demo_store(store)
    scan = Scanner(graph, store).scan("all")
    posted = []
    actions = Actions(graph, store, poster=lambda u: (posted.append(u), (True, "ok"))[1])
    return graph, store, scan, actions, posted


def test_the_table_itself_never_lets_a_risky_sender_unsubscribe():
    for kind in RISKY:
        assert ALLOWED_ACTIONS[kind] == {"keep", "block"}, kind


@pytest.mark.parametrize("forbidden", ["unsub", "unsubdel"])
def test_the_server_refuses_even_when_the_ui_is_bypassed(env, forbidden):
    graph, store, scan, actions, posted = env
    risky = [s for s in scan.senders if s["kind"] in RISKY]
    assert risky, "the demo needs a suspicious sender for this to mean anything"

    results = actions.apply({s["id"]: forbidden for s in risky}, scan)

    assert len(results) == len(risky)
    assert all(r["status"] == "failed" for r in results), "a forbidden action was carried out"
    assert not posted, "an unsubscribe was sent to a suspicious sender"
    # ...and their mail stayed put, so "unsubdel" didn't purge by the back door.
    addresses = {s["address"] for s in risky}
    stray = [m for m in graph.messages
             if m["from"]["emailAddress"]["address"] in addresses
             and m["parentFolderId"] not in ("f-inbox", "f-junk")]
    assert not stray, f"{len(stray)} emails were moved for a suspicious sender"


def test_the_refusal_says_what_to_do_instead(env):
    graph, store, scan, actions, posted = env
    risky = next(s for s in scan.senders if s["kind"] in RISKY)
    [result] = actions.apply({risky["id"]: "unsub"}, scan)
    assert "block" in result["outcome"].lower(), "refused without offering the safe option"


def test_blocking_a_suspicious_sender_still_works(env):
    """The refusal must not be so broad that the one safe action breaks too."""
    graph, store, scan, actions, posted = env
    risky = next(s for s in scan.senders if s["kind"] in RISKY)
    [result] = actions.apply({risky["id"]: "block"}, scan)
    assert result["status"] == "done"
    assert risky["address"] in store.blocked()
