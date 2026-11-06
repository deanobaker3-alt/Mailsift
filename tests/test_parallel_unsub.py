"""One-click unsubscribes go out together, not one at a time.

They're HTTPS POSTs to the senders' own servers, each waiting up to 15
seconds, so a clean-up of ninety senders spent most of its time queueing
for dead endpoints. Outlook's per-mailbox ceiling doesn't apply to them.
Only the asking is parallel: everything that changes the mailbox still
happens in order.
"""
import threading
import time

import pytest

from mailsift.actions import ONE_CLICK_WORKERS, Actions
from mailsift.demo import DemoGraph, seed_demo_store
from mailsift.scanner import Scanner
from mailsift.store import Store


class Senders:
    """Stand-ins for the senders' servers, noting how many are asked at once."""

    def __init__(self, delay=0.05, dead=(), explode=()):
        self.delay, self.dead, self.explode = delay, set(dead), set(explode)
        self.lock = threading.Lock()
        self.live = self.peak = 0
        self.asked = []

    def __call__(self, url):
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
            self.asked.append(url)
        try:
            time.sleep(self.delay)
            if url in self.explode:
                raise ConnectionResetError("rude server")
            if url in self.dead:
                return False, "Couldn't reach the sender (SSLError)"
            return True, "HTTP 202"
        finally:
            with self.lock:
                self.live -= 1


@pytest.fixture
def mailbox():
    graph, store = DemoGraph(delay=0), Store(":memory:")
    seed_demo_store(store)
    scan = Scanner(graph, store).scan("all")
    oneclick = [s for s in scan.senders if s["kind"] == "oneclick"]
    assert len(oneclick) >= 3, "need a few one-click senders to test against"
    urls = {scan.private[s["id"]]["https"][0] for s in oneclick}
    return graph, store, scan, oneclick, urls


def test_senders_are_asked_at_the_same_time(mailbox):
    graph, store, scan, oneclick, urls = mailbox
    post = Senders()
    Actions(graph, store, poster=post).apply({s["id"]: "unsub" for s in oneclick}, scan)
    assert post.peak > 1, "still asking one sender at a time"
    assert post.peak <= ONE_CLICK_WORKERS


def test_it_is_actually_faster(mailbox):
    graph, store, scan, oneclick, urls = mailbox
    post = Senders(delay=0.08)
    start = time.perf_counter()
    Actions(graph, store, poster=post).apply({s["id"]: "unsub" for s in oneclick}, scan)
    took = time.perf_counter() - start
    assert took < len(oneclick) * 0.08 * 0.7, f"{took:.2f}s for {len(oneclick)} senders"


def test_each_sender_still_gets_its_own_answer(mailbox):
    graph, store, scan, oneclick, urls = mailbox
    dead_url = scan.private[oneclick[0]["id"]]["https"][0]
    results = Actions(graph, store, poster=Senders(dead={dead_url})).apply(
        {s["id"]: "unsub" for s in oneclick}, scan)
    by_id = {r["id"]: r for r in results}
    # Answers must land against the sender they belong to, not whichever
    # finished first. Only senders on the dead endpoint miss the one-click.
    for s in oneclick:
        one_click_worked = "one-click" in by_id[s["id"]]["outcome"]
        shares_dead = scan.private[s["id"]]["https"][0] == dead_url
        assert one_click_worked is not shares_dead, s["address"]


def test_a_server_that_misbehaves_doesnt_take_the_run_down(mailbox):
    graph, store, scan, oneclick, urls = mailbox
    rude = scan.private[oneclick[0]["id"]]["https"][0]
    results = Actions(graph, store, poster=Senders(explode={rude})).apply(
        {s["id"]: "unsub" for s in oneclick}, scan)
    assert len(results) == len(oneclick), "a rude server must not cost the other senders"
    bad = next(r for r in results if r["id"] == oneclick[0]["id"])
    assert "one-click" not in bad["outcome"], "a thrown exception counted as success"


def test_results_come_back_in_the_order_chosen(mailbox):
    graph, store, scan, oneclick, urls = mailbox
    chosen = {s["id"]: "unsub" for s in oneclick}
    results = Actions(graph, store, poster=Senders()).apply(chosen, scan)
    assert [r["id"] for r in results] == list(chosen)


def test_an_endpoint_shared_by_two_senders_is_asked_only_once(mailbox):
    # Two of the demo's senders are the same company and publish the same
    # unsubscribe link. Asking it twice would be a wasted round trip.
    graph, store, scan, oneclick, urls = mailbox
    assert len(urls) < len(oneclick), "the demo needs a shared endpoint for this to mean anything"
    post = Senders()
    Actions(graph, store, poster=post).apply({s["id"]: "unsub" for s in oneclick}, scan)
    assert sorted(post.asked) == sorted(urls)


def test_a_dry_run_asks_nobody(mailbox):
    graph, store, scan, oneclick, urls = mailbox
    post = Senders()
    Actions(graph, store, dry_run=True, poster=post).apply({s["id"]: "unsub" for s in oneclick}, scan)
    assert post.asked == []


def test_protected_senders_are_never_asked(mailbox):
    graph, store, scan, oneclick, urls = mailbox
    alone = next(s for s in oneclick
                 if sum(1 for o in oneclick
                        if scan.private[o["id"]]["https"][0] == scan.private[s["id"]]["https"][0]) == 1)
    store.set_protected(alone["address"], True)
    post = Senders()
    Actions(graph, store, poster=post).apply({s["id"]: "unsub" for s in oneclick}, scan)
    assert scan.private[alone["id"]]["https"][0] not in post.asked


def test_progress_says_what_is_happening_while_they_are_asked(mailbox):
    graph, store, scan, oneclick, urls = mailbox
    notes = []
    Actions(graph, store, poster=Senders()).apply(
        {s["id"]: "unsub" for s in oneclick}, scan,
        progress=lambda **kw: notes.append(kw.get("note")))
    said = [n for n in notes if n]
    assert said and "unsubscribe" in said[0], "the bar would sit at zero with no explanation"
    assert said[-1].endswith(f"{len(urls)} of {len(urls)}")


def test_the_note_is_up_before_the_waiting_starts(mailbox):
    """The defect this guards: the note was posted only as each answer came
    back, and the answers all land together at the end. The bar sat at zero
    with nothing on screen for the whole wait — the exact thing the note
    exists to prevent. Driving the real app was the only way this showed up;
    a test that only checked the notes afterwards passed happily."""
    graph, store, scan, oneclick, urls = mailbox
    released = threading.Event()
    asked = threading.Event()
    notes = []

    def blocking_poster(url):
        asked.set()
        released.wait(10)
        return True, "HTTP 202"

    def progress(done, total, result=None, note=None):
        if note:
            notes.append(note)

    actions = Actions(graph, store, poster=blocking_poster)
    run = threading.Thread(
        target=actions.apply,
        args=({s["id"]: "unsub" for s in oneclick}, scan),
        kwargs={"progress": progress}, daemon=True)
    run.start()
    assert asked.wait(10), "the senders were never asked"
    # Nothing has answered yet, and the user must already see why they wait.
    assert notes, "nothing on screen while every sender is being waited on"
    assert "unsubscribe" in notes[0].lower()
    released.set()
    run.join(20)
    assert not run.is_alive()
