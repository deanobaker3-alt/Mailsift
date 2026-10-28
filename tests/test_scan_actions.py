import pytest

from mailsift.actions import Actions
from mailsift.demo import FOLDERS, IGNORED_SENDER, DemoGraph, seed_demo_store
from mailsift.scanner import Scanner, sender_id
from mailsift.store import Store


@pytest.fixture
def env():
    graph = DemoGraph(delay=0)
    store = Store(":memory:")
    seed_demo_store(store)
    posted = []

    def poster(url):
        posted.append(url)
        return True, "HTTP 200"

    return graph, store, posted, poster


def by_address(result):
    return {s["address"]: s for s in result.senders}


def test_scan_groups_and_classifies(env):
    graph, store, _, _ = env
    result = Scanner(graph, store).scan("12m")
    senders = by_address(result)
    assert "mum@example.co.uk" not in senders  # personal mail is never listed
    assert senders["offers@crustandco.co.uk"]["count"] == 214
    assert senders["offers@crustandco.co.uk"]["kind"] == "oneclick"
    assert senders["news@fitfuel.co.uk"]["kind"] == "mailto"
    assert senders["team@gearhub.co.uk"]["kind"] == "link"
    assert senders["claim@amaz0n-prize.top"]["kind"] == "dodgy"
    assert senders["track@parcel-redeliver.xyz"]["kind"] == "dodgy"
    assert senders[IGNORED_SENDER]["ignored"] is True
    assert senders["offers@crustandco.co.uk"]["ignored"] is False
    assert result.senders[0]["address"] == "offers@crustandco.co.uk"  # biggest first
    # message ids and URLs stay server-side
    assert "ids" not in result.senders[0]


def test_sent_and_deleted_are_skipped(env):
    graph, store, _, _ = env
    crust = [m for m in graph.messages if m["from"]["emailAddress"]["address"] == "offers@crustandco.co.uk"]
    graph.move_messages([m["id"] for m in crust[:14]], FOLDERS["deleteditems"])
    result = Scanner(graph, store).scan("12m")
    assert by_address(result)["offers@crustandco.co.uk"]["count"] == 200


def test_apply_all_paths(env):
    graph, store, posted, poster = env
    scan = Scanner(graph, store).scan("12m")
    choices = {
        sender_id("offers@crustandco.co.uk"): "unsubdel",
        sender_id("news@fitfuel.co.uk"): "unsub",
        sender_id("team@gearhub.co.uk"): "unsub",
        sender_id("claim@amaz0n-prize.top"): "block",
        sender_id("newsletter@weeklybyte.io"): "keep",
    }
    results = {r["address"]: r for r in Actions(graph, store, poster=poster).apply(choices, scan)}

    assert posted == ["https://unsub.crustandco.co.uk/u?id=demo"]
    assert results["offers@crustandco.co.uk"]["status"] == "done"
    assert "Moved 214 old emails to Deleted Items" in results["offers@crustandco.co.uk"]["outcome"]
    crust = [m for m in graph.messages if m["from"]["emailAddress"]["address"] == "offers@crustandco.co.uk"]
    assert all(m["parentFolderId"] == FOLDERS["deleteditems"] for m in crust)

    assert graph.sent == [{"to": "leave@fitfuel.co.uk", "subject": "unsubscribe", "body": "unsubscribe"}]
    assert results["team@gearhub.co.uk"]["status"] == "needs_you"
    assert results["team@gearhub.co.uk"]["link"] == "https://www.gearhub.co.uk/preferences"

    assert results["claim@amaz0n-prize.top"]["status"] == "done"
    assert graph.rules[0]["conditions"] == {"senderContains": ["claim@amaz0n-prize.top"]}
    assert graph.rules[0]["actions"]["moveToFolder"] == FOLDERS["junkemail"]
    assert "newsletter@weeklybyte.io" not in results  # keep = untouched

    assert "offers@crustandco.co.uk" in store.unsubscribed()
    assert "claim@amaz0n-prize.top" in store.blocked()
    assert len(store.history()) == 4


def test_dodgy_senders_can_never_be_unsubscribed(env):
    graph, store, posted, poster = env
    scan = Scanner(graph, store).scan("12m")
    results = Actions(graph, store, poster=poster).apply({sender_id("claim@amaz0n-prize.top"): "unsub"}, scan)
    assert results[0]["status"] == "failed"
    assert posted == []


def test_dry_run_changes_nothing(env):
    graph, store, posted, poster = env
    scan = Scanner(graph, store).scan("12m")
    before = [m["parentFolderId"] for m in graph.messages]
    choices = {sender_id("offers@crustandco.co.uk"): "unsubdel", sender_id("news@fitfuel.co.uk"): "unsub",
               sender_id("claim@amaz0n-prize.top"): "block"}
    results = Actions(graph, store, dry_run=True, poster=poster).apply(choices, scan)
    assert all(r["status"] == "dry_run" for r in results)
    assert posted == [] and graph.sent == [] and graph.rules == []
    assert [m["parentFolderId"] for m in graph.messages] == before
    assert "offers@crustandco.co.uk" not in store.unsubscribed()


def test_block_twice_reuses_rule(env):
    graph, store, _, poster = env
    scan = Scanner(graph, store).scan("12m")
    actions = Actions(graph, store, poster=poster)
    actions.apply({sender_id("claim@amaz0n-prize.top"): "block"}, scan)
    actions.apply({sender_id("claim@amaz0n-prize.top"): "block"}, scan)
    assert len(graph.rules) == 1


def test_failed_one_click_falls_back_to_mailto(env):
    graph, store, _, _ = env
    scan = Scanner(graph, store).scan("12m")
    results = Actions(graph, store, poster=lambda url: (False, "HTTP 500")).apply(
        {sender_id("offers@crustandco.co.uk"): "unsub"}, scan)
    assert results[0]["status"] == "done"
    assert graph.sent[0]["to"] == "unsub@crustandco.co.uk"


def test_parallel_scan_matches_single_worker(env):
    graph, store, _, _ = env
    one = by_address(Scanner(graph, store, workers=1).scan("all"))
    four = by_address(Scanner(graph, store, workers=4).scan("all"))
    assert one.keys() == four.keys()
    for address in one:
        for key in ("count", "read", "kind", "last", "first", "auth", "ignored"):
            assert one[address][key] == four[address][key], (address, key)


def test_unsubscribe_details_come_from_newest_email(env):
    graph, store, _, _ = env
    msgs = [m for m in graph.messages if m["from"]["emailAddress"]["address"] == "team@gearhub.co.uk"]
    oldest = min(msgs, key=lambda m: m["receivedDateTime"])
    oldest["internetMessageHeaders"] = [h for h in oldest["internetMessageHeaders"] if h["name"] != "List-Unsubscribe"] + [
        {"name": "List-Unsubscribe", "value": "<https://stale.gearhub.co.uk/old>"}]
    scan = Scanner(graph, store, workers=4).scan("12m")
    private = next(p for p in scan.private.values() if p["address"] == "team@gearhub.co.uk")
    assert private["https"] == ["https://www.gearhub.co.uk/preferences"]


def test_per_month_and_auth_exposed(env):
    graph, store, _, _ = env
    s = by_address(Scanner(graph, store).scan("12m"))["offers@crustandco.co.uk"]
    assert 10 < s["per_month"] < 40   # 214 emails over roughly a year
    assert s["auth"]["dmarc"] == "pass"
    assert by_address(Scanner(graph, store).scan("12m"))["claim@amaz0n-prize.top"]["auth"]["dmarc"] == "fail"
