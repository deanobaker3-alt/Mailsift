"""v1.3: merged blocklist rules, undo, never-touch list."""
import time

import pytest

from mailsift.actions import Actions
from mailsift.blocklist import BLOCKLIST_NAME, CHUNK, Blocklist
from mailsift.demo import FOLDERS, DemoGraph, seed_demo_store
from mailsift.scanner import Scanner, sender_id
from mailsift.store import Store

CRUST, FIT, AMZ, PARCEL = ("offers@crustandco.co.uk", "news@fitfuel.co.uk",
                           "claim@amaz0n-prize.top", "track@parcel-redeliver.xyz")


@pytest.fixture
def env():
    graph = DemoGraph(delay=0)
    store = Store(":memory:")
    seed_demo_store(store)
    actions = Actions(graph, store, poster=lambda url: (True, "HTTP 202"))
    return graph, store, actions


def scan(graph, store):
    return Scanner(graph, store).scan("all")


def folder_of(graph, address):
    return {m["parentFolderId"] for m in graph.messages if m["from"]["emailAddress"]["address"] == address}


def rule_addresses(graph):
    out = []
    for r in graph.rules:
        out += [a["emailAddress"]["address"] for a in r["conditions"].get("fromAddresses", [])]
    return sorted(out)


# -- merged blocklist ------------------------------------------------------------
def test_blocks_share_one_rule(env):
    graph, store, actions = env
    actions.apply({sender_id(AMZ): "block", sender_id(PARCEL): "block"}, scan(graph, store))
    assert len(graph.rules) == 1
    assert graph.rules[0]["displayName"] == BLOCKLIST_NAME
    assert rule_addresses(graph) == sorted([AMZ, PARCEL])
    # a later clean-up extends the same rule rather than adding another
    actions.apply({sender_id(CRUST): "block"}, scan(graph, store))
    assert len(graph.rules) == 1 and rule_addresses(graph) == sorted([AMZ, PARCEL, CRUST])


def test_legacy_per_sender_rules_are_folded_in(env):
    graph, store, actions = env
    graph.rules = [{"id": "old-1", "displayName": "MailSift block: old@spam.example", "sequence": 3,
                    "conditions": {"fromAddresses": [{"emailAddress": {"address": "old@spam.example"}}]}},
                   {"id": "user-1", "displayName": "My own rule", "sequence": 1, "conditions": {}}]
    actions.apply({sender_id(AMZ): "block"}, scan(graph, store))
    names = sorted(r["displayName"] for r in graph.rules)
    assert names == [BLOCKLIST_NAME, "My own rule"]         # user's own rule untouched
    assert rule_addresses(graph) == sorted([AMZ, "old@spam.example"])


def test_big_blocklist_is_chunked():
    graph = DemoGraph(delay=0)
    addresses = [f"s{i}@spam.example" for i in range(CHUNK * 2 + 5)]
    Blocklist(graph).sync(add=addresses)
    assert len(graph.rules) == 3 and len(rule_addresses(graph)) == len(addresses)
    Blocklist(graph).sync(remove=addresses[:50])            # 155 left -> 2 rules
    assert len(graph.rules) == 2 and len(rule_addresses(graph)) == 155
    Blocklist(graph).sync(remove=addresses[:CHUNK + 10])    # 95 left -> 1 rule
    assert len(graph.rules) == 1
    Blocklist(graph).sync(remove=addresses)
    assert graph.rules == []


def test_dry_run_writes_no_rules(env):
    graph, store, _ = env
    Actions(graph, store, dry_run=True).apply({sender_id(AMZ): "block"}, scan(graph, store))
    assert graph.rules == [] and store.last_undoable() is None


# -- undo --------------------------------------------------------------------------
def test_undo_restores_mail_and_lifts_blocks(env):
    graph, store, actions = env
    before = {a: folder_of(graph, a) for a in (CRUST, AMZ, PARCEL)}
    actions.apply({sender_id(CRUST): "unsubdel", sender_id(AMZ): "block", sender_id(PARCEL): "block"},
                  scan(graph, store))
    assert folder_of(graph, CRUST) == {FOLDERS["deleteditems"]}
    assert folder_of(graph, AMZ) == {FOLDERS["junkemail"]}

    last = store.last_undoable()
    assert last["moved"] == 214 + 12 + 9 and last["blocked"] == 2
    summary = actions.undo(last["run_id"])
    assert summary == {"restored": 235, "failed": 0, "unblocked": 2, "block_error": None}
    assert {a: folder_of(graph, a) for a in (CRUST, AMZ, PARCEL)} == before   # back where they were
    assert graph.rules == [] and AMZ not in store.blocked()
    assert store.last_undoable() is None
    assert CRUST in store.unsubscribed()   # unsubscribes can't be undone, and that's recorded honestly


def test_undo_keeps_earlier_blocks(env):
    graph, store, actions = env
    actions.apply({sender_id(AMZ): "block"}, scan(graph, store))
    actions.apply({sender_id(PARCEL): "block"}, scan(graph, store))
    actions.undo(store.last_undoable()["run_id"])
    assert rule_addresses(graph) == [AMZ]   # only the latest run's block was lifted


# -- never-touch list -----------------------------------------------------------
def test_protected_sender_is_never_touched(env):
    graph, store, actions = env
    store.set_protected(CRUST, True)
    result = scan(graph, store)
    assert next(s for s in result.senders if s["address"] == CRUST)["protected"] is True
    before = folder_of(graph, CRUST)
    posted = []
    Actions(graph, store, poster=lambda u: (posted.append(u), (True, "ok"))[1]).apply(
        {sender_id(CRUST): "unsubdel"}, result)
    assert posted == [] and folder_of(graph, CRUST) == before
    store.set_protected(CRUST, False)
    assert CRUST not in store.protected()


def test_forget_wipes_new_tables(env):
    graph, store, actions = env
    store.set_protected(CRUST, True)
    actions.apply({sender_id(AMZ): "block"}, scan(graph, store))
    store.wipe()
    assert store.protected() == set() and store.last_undoable() is None
