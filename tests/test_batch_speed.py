"""$batch chunks go out concurrently.

A clean-up of one busy sender can be thousands of moves, which is a hundred
chunks of twenty. Sent one after another that was minutes of pure waiting.
Order and per-message results must survive the change: undo depends on
pairing each old message id with the new one Outlook gives it.
"""
import threading
import time

import pytest

from mailsift.graph import GraphClient


class Recorder:
    """A Graph that notes how many $batch calls overlap."""

    def __init__(self, delay=0.02, fail_ids=(), throttle_first=False):
        self.delay, self.fail_ids, self.throttle_first = delay, set(fail_ids), throttle_first
        self.lock = threading.Lock()
        self.live = self.peak = self.calls = 0
        self.seen_429 = False

    def request(self, method, url, **kw):
        with self.lock:
            self.live += 1
            self.calls += 1
            self.peak = max(self.peak, self.live)
        try:
            time.sleep(self.delay)
            out = []
            for req in kw["json"]["requests"]:
                mid = req["url"].rsplit("/", 2)[1]
                if self.throttle_first and not self.seen_429:
                    self.seen_429 = True
                    out.append({"id": req["id"], "status": 429})
                elif mid in self.fail_ids:
                    out.append({"id": req["id"], "status": 404, "body": {}})
                else:
                    out.append({"id": req["id"], "status": 201, "body": {"id": f"new-{mid}"}})
            return {"responses": out}
        finally:
            with self.lock:
                self.live -= 1


def client(rec):
    g = GraphClient(lambda: "t", session_factory=lambda: None, sleep=lambda s: None)
    g.request = rec.request
    return g


def test_chunks_overlap_instead_of_queueing():
    rec = Recorder()
    g = client(rec)
    ids = [f"m{i}" for i in range(200)]       # 10 chunks of 20
    g.move_messages(ids, "junkemail")
    assert rec.calls == 10
    assert rec.peak > 1, "chunks were still sent one at a time"
    assert rec.peak <= GraphClient.CONCURRENCY, "went over Outlook's per-mailbox ceiling"


def test_it_is_actually_faster():
    ids = [f"m{i}" for i in range(400)]       # 20 chunks
    rec = Recorder(delay=0.03)
    start = time.perf_counter()
    client(rec).move_messages(ids, "junkemail")
    took = time.perf_counter() - start
    sequential = 20 * 0.03
    assert took < sequential * 0.6, f"{took:.2f}s vs {sequential:.2f}s sequential"


def test_results_stay_in_order_so_undo_still_works():
    ids = [f"m{i:03d}" for i in range(150)]
    moved, failed = client(Recorder()).move_messages(ids, "deleteditems")
    assert failed == 0
    assert [old for old, _ in moved] == ids, "old ids must come back in the order given"
    assert all(new == f"new-{old}" for old, new in moved), "each old id paired with its own new one"


def test_one_failure_among_many_is_reported_against_the_right_message():
    ids = [f"m{i:03d}" for i in range(100)]
    rec = Recorder(fail_ids={"m042", "m077"})
    moved, failed = client(rec).move_messages(ids, "junkemail")
    assert failed == 2
    assert {old for old, _ in moved} == set(ids) - {"m042", "m077"}


def test_throttling_inside_a_chunk_is_still_retried():
    rec = Recorder(throttle_first=True)
    moved, failed = client(rec).move_messages([f"m{i}" for i in range(40)], "junkemail")
    assert rec.seen_429 and failed == 0 and len(moved) == 40


@pytest.mark.parametrize("count", [0, 1, 19, 20, 21])
def test_small_and_edge_sized_moves(count):
    ids = [f"m{i}" for i in range(count)]
    moved, failed = client(Recorder(delay=0)).move_messages(ids, "junkemail")
    assert len(moved) == count and failed == 0
