"""One malformed sender must not fail every block in a clean-up.

From a real run: a sender's address arrived as "<w3a9h5h2w3@sodome-bisam
.assumunions.com>", angle brackets included. MailSift put it straight into
the merged blocklist rule, Outlook rejected the whole rule with
InvalidAddress, and all 66 blocks in that clean-up failed at once.
"""
import pytest

from mailsift.actions import Actions
from mailsift.blocklist import Blocklist, usable
from mailsift.demo import DemoGraph, seed_demo_store
from mailsift.graph import GraphError
from mailsift.headers import normalise_address
from mailsift.scanner import Scanner
from mailsift.store import Store

BAD = "<w3a9h5h2w3@sodome-bisam.assumunions.com>"
CLEAN = "w3a9h5h2w3@sodome-bisam.assumunions.com"


# -- cleaning at the door ----------------------------------------------------------
@pytest.mark.parametrize("given,expected", [
    (BAD, CLEAN),
    ("  News@Brand.CO.UK  ", "news@brand.co.uk"),
    ('"Some Name" <a@b.com>', "a@b.com"),
    ("<<a@b.com>>", "a@b.com"),
    ("a@b.com", "a@b.com"),
    ("not an address", ""),
    ("", ""),
    (None, ""),
    ("@nope", ""),
])
def test_addresses_are_cleaned_up(given, expected):
    assert normalise_address(given) == expected


@pytest.mark.parametrize("bad", ["x" * 250 + "@brand.com", "ünïcode@brand.com", "no-at-sign", ""])
def test_outlook_unsafe_addresses_are_recognised(bad):
    assert usable(bad) == ""


def test_a_bracketed_address_is_repaired_rather_than_discarded():
    # Better than refusing it: the sender really does get blocked.
    assert usable(BAD) == CLEAN


def test_the_scan_never_produces_a_bracketed_address():
    graph, store = DemoGraph(delay=0), Store(":memory:")
    for msg in graph.messages[:30]:
        msg["from"]["emailAddress"]["address"] = BAD
    result = Scanner(graph, store).scan("all")
    addresses = [s["address"] for s in result.senders]
    assert CLEAN in addresses and BAD not in addresses
    assert all("<" not in a and ">" not in a and a == a.strip() for a in addresses)


# -- one bad address costs only itself ----------------------------------------------
class PickyGraph(DemoGraph):
    """Outlook as it really behaved: any rule containing the bad value is
    refused outright, with the offending value quoted back."""

    def _check(self, body):
        for entry in (body.get("conditions") or {}).get("fromAddresses") or []:
            value = entry["emailAddress"]["address"]
            if value != normalise_address(value):
                raise GraphError(400, f"ErrorCode: 'InvalidAddress', Message: 'The address isn't "
                                      f"valid'; Field: 'Condition.FromAddresses'; Value: '\"{value}\"'")

    def create_rule(self, body):
        self._check(body)
        return super().create_rule(body)

    def update_rule(self, rule_id, body):
        self._check(body)
        return super().update_rule(rule_id, body)


def test_a_malformed_address_is_left_out_rather_than_failing_the_rule():
    graph = PickyGraph(delay=0)
    good = [f"sender{i}@brand{i}.com" for i in range(20)]
    hopeless = "nonsense-no-at-sign"
    blocked, rejected = Blocklist(graph).sync(add=good + [BAD, hopeless])
    assert blocked == set(good) | {CLEAN}, "the bracketed one is repaired, the rest unaffected"
    assert list(rejected) == [hopeless], "only the truly unusable one is left out"
    assert "won't accept" in rejected[hopeless]


def test_a_whole_rule_being_refused_costs_only_that_rule():
    # Something Outlook dislikes for a reason we can't predict: the other
    # rules still get written.
    graph = PickyGraph(delay=0)
    addresses = [f"s{i:04d}@brand.com" for i in range(250)]   # 3 rules
    original = graph.create_rule
    calls = {"n": 0}

    def flaky(body):
        calls["n"] += 1
        if calls["n"] == 2:
            raise GraphError(400, "ErrorCode: 'InvalidAddress'")
        return original(body)

    graph.create_rule = flaky
    blocked, rejected = Blocklist(graph).sync(add=addresses)
    assert len(blocked) == 150 and len(rejected) == 100
    assert set(blocked) | set(rejected) == set(addresses), "every sender accounted for"


def test_an_unusable_address_already_in_a_rule_doesnt_break_later_writes():
    graph = DemoGraph(delay=0)
    graph.rules.append({"id": "old", "displayName": "MailSift blocklist", "sequence": 1,
                        "isEnabled": True, "actions": {},
                        "conditions": {"fromAddresses": [{"emailAddress": {"address": BAD}}]}})
    blocked, rejected = Blocklist(PickyGraph.__new__(PickyGraph) or graph).sync(add=["new@brand.com"]) \
        if False else Blocklist(graph).sync(add=["new@brand.com"])
    assert CLEAN in blocked and "new@brand.com" in blocked and not rejected


# -- what the user is told -----------------------------------------------------------
def test_the_other_senders_are_still_blocked_and_the_odd_one_is_explained():
    graph, store = PickyGraph(delay=0), Store(":memory:")
    seed_demo_store(store)
    scan = Scanner(graph, store).scan("all")
    # make one scanned sender unblockable the way the real one was
    scan.private[scan.senders[0]["id"]]["address"] = BAD          # repairable
    scan.private[scan.senders[1]["id"]]["address"] = "nonsense"    # not
    targets = [s["id"] for s in scan.senders[:6]]

    results = Actions(graph, store).apply({i: "block" for i in targets}, scan)
    done = [r for r in results if r["status"] == "done"]
    failed = [r for r in results if r["status"] == "failed"]

    assert len(done) == 5, "one odd sender must not take the others with it"
    assert len(failed) == 1 and failed[0]["address"] == "nonsense"
    assert "won't be blocked" in failed[0]["outcome"]
    assert "Junk" in failed[0]["outcome"], "say the mail still moved, or it reads as nothing happened"
    assert CLEAN in store.blocked(), "stored as it went into the rule, not as the sender wrote it"
    assert BAD not in store.blocked()


def test_undo_still_lifts_the_blocks_that_worked():
    graph, store = PickyGraph(delay=0), Store(":memory:")
    seed_demo_store(store)
    scan = Scanner(graph, store).scan("all")
    scan.private[scan.senders[0]["id"]]["address"] = "nonsense"
    Actions(graph, store).apply({s["id"]: "block" for s in scan.senders[:6]}, scan)
    run = store.last_undoable()
    assert run and run["blocked"] == 5
    assert Actions(graph, store).undo(run["run_id"])["unblocked"] == 5
    assert store.blocked() == set()
