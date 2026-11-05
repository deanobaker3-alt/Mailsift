"""Findings from a read-through of the whole codebase, each pinned by a test
that fails against the code as it was."""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest

from mailsift.actions import Actions
from mailsift.demo import DemoGraph, seed_demo_store
from mailsift.graph import GraphError
from mailsift.scanner import Scanner, split_windows, window_count
from mailsift.store import LOG_KEEP, Store


@pytest.fixture
def world():
    graph, store = DemoGraph(delay=0), Store(":memory:")
    seed_demo_store(store)
    scan = Scanner(graph, store).scan("12m")
    return graph, store, scan, Actions(graph, store, poster=lambda url: (True, "HTTP 202"))


# -- one sender's failure must not abandon the rest -------------------------------
def test_an_unexpected_error_on_one_sender_doesnt_abort_the_clean_up(world):
    graph, store, scan, actions = world
    ids = [s["id"] for s in scan.senders[:6]]
    real, calls = graph.move_messages, {"n": 0}

    def flaky(message_ids, destination):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TimeoutError("network hiccup")
        return real(message_ids, destination)

    graph.move_messages = flaky
    results = actions.apply({i: "unsubdel" for i in ids}, scan)

    assert len(results) == len(ids), "every sender must get a result"
    assert sum(r["status"] == "failed" for r in results) == 1
    assert sum(r["status"] == "done" for r in results) == len(ids) - 1
    bad = next(r for r in results if r["status"] == "failed")
    assert "TimeoutError" in bad["outcome"] and "other senders were still done" in bad["outcome"]


def test_graph_errors_are_still_reported_per_sender(world):
    graph, store, scan, actions = world
    graph.move_messages = lambda ids, dest: (_ for _ in ()).throw(GraphError(503, "busy"))
    results = actions.apply({s["id"]: "unsubdel" for s in scan.senders[:3]}, scan)
    assert len(results) == 3 and all(r["status"] == "failed" for r in results)
    assert all("503" in r["outcome"] or "busy" in r["outcome"] for r in results)


# -- undo records are created only when there's something to undo ------------------
def test_unsubscribe_only_clean_ups_leave_no_empty_undo_records(world):
    graph, store, scan, actions = world
    mailto = next(s for s in scan.senders if s["kind"] == "mailto")
    for _ in range(5):
        Actions(graph, store, poster=lambda url: (True, "ok")).apply({mailto["id"]: "unsub"}, scan)
    assert store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert store.last_undoable() is None


def test_a_clean_up_that_moves_mail_is_still_undoable(world):
    graph, store, scan, actions = world
    actions.apply({scan.senders[0]["id"]: "unsubdel"}, scan)
    undo = store.last_undoable()
    assert undo and undo["moved"] > 0
    assert store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_blocking_is_still_undoable(world):
    graph, store, scan, actions = world
    dodgy = next(s for s in scan.senders if s["kind"] == "dodgy")
    actions.apply({dodgy["id"]: "block"}, scan)
    undo = store.last_undoable()
    assert undo and undo["blocked"] == 1


def test_a_dry_run_records_nothing(world):
    graph, store, scan, _ = world
    Actions(graph, store, dry_run=True, poster=lambda url: (True, "ok")).apply(
        {s["id"]: "unsubdel" for s in scan.senders[:3]}, scan)
    assert store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


# -- the local action log is bounded ------------------------------------------------
def test_the_action_log_stops_growing(tmp_path):
    path = tmp_path / "m.db"
    store = Store(path)
    for i in range(LOG_KEEP + 350):
        store.log(f"a{i}@x.com", "X", "unsub", "done", "detail")
    reopened = Store(path)   # trimmed when it's next opened
    kept = reopened.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    assert kept == LOG_KEEP
    newest = reopened.history(1)[0]
    assert newest["address"] == f"a{LOG_KEEP + 349}@x.com", "the newest entries are the ones kept"


def test_trimming_leaves_the_rest_of_the_database_alone(tmp_path):
    path = tmp_path / "m.db"
    store = Store(path)
    store.mark_unsubscribed("keep@me.com", "mailto")
    store.mark_blocked("blocked@me.com")
    store.set_protected("safe@me.com", True)
    for i in range(LOG_KEEP + 50):
        store.log(f"a{i}@x.com", "X", "unsub", "done", "")
    again = Store(path)
    assert "keep@me.com" in again.unsubscribed()
    assert again.blocked() == {"blocked@me.com"} and again.protected() == {"safe@me.com"}


# -- config survives a crash mid-write -----------------------------------------------
def test_config_is_written_atomically(tmp_path):
    from mailsift.app import AppState
    st = AppState(tmp_path, demo=False)
    st.save_client_id("1a2b3c4d-1111-2222-3333-444455556666")
    st.save_theme("ops")
    assert json.loads((tmp_path / "config.json").read_text())["theme"] == "ops"
    assert not list(tmp_path.glob("*.tmp")), "no temporary file left behind"
    # a half-written file is never what readers see: the real file is only
    # ever replaced whole
    seen = []
    original = os.replace

    def watch(src, dst):
        seen.append(json.loads(open(src).read()))
        return original(src, dst)

    import mailsift.app as app_module
    app_module.os.replace = watch
    try:
        st.save_theme("blueprint")
    finally:
        app_module.os.replace = original
    assert seen and seen[0]["client_id"] and seen[0]["theme"] == "blueprint"


def test_concurrent_saves_dont_lose_each_other(tmp_path):
    from mailsift.app import AppState
    st = AppState(tmp_path, demo=False)
    st.save_client_id("1a2b3c4d-1111-2222-3333-444455556666")
    errors = []

    def spin(theme):
        try:
            for _ in range(40):
                st.save_theme(theme)
        except Exception as exc:   # a torn read would raise here
            errors.append(exc)

    threads = [threading.Thread(target=spin, args=(t,)) for t in ("ops", "blueprint", "retro")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["client_id"] == "1a2b3c4d-1111-2222-3333-444455556666"
    assert cfg["theme"] in ("ops", "blueprint", "retro")


# -- scan windows are cut finely enough to share the work ----------------------------
def test_window_count_adapts_to_the_size_of_the_mailbox():
    assert window_count(None, 4) == 4        # size unknown: don't pay for empty windows
    assert window_count(0, 4) == 4
    assert window_count(600, 4) == 4         # small: one per worker is plenty
    assert window_count(26000, 4) == 32      # big: capped so the overhead stays small
    assert window_count(10**9, 4) == 32
    for total in (1, 500, 5000, 50000):
        assert window_count(total, 4) % 1 == 0 and window_count(total, 4) >= 4


def test_finer_windows_even_out_a_realistically_lopsided_mailbox():
    # Mailboxes get busier with age, so equal-time windows are badly unequal
    # in volume. Measured: the newest quarter held 53% of two years of mail.
    import random
    rng = random.Random(5)
    now = datetime.now(timezone.utc)
    ages = [730 * (rng.random() ** 2.2) for _ in range(40000)]
    start, end = now - timedelta(days=730), now

    def busiest_share(parts):
        edges = split_windows(start, end, parts)
        counts = [sum(1 for a in ages if lo <= now - timedelta(days=a) < hi) for lo, hi in edges]
        # four workers take windows as they free up, so the slowest finishes
        # the largest total any one worker could be left holding
        loads = [0] * 4
        for c in sorted(counts, reverse=True):
            loads[loads.index(min(loads))] += c
        return max(loads) / sum(counts)

    before = busiest_share(4)
    after = busiest_share(window_count(40000, 4))
    assert before > 0.45, f"expected the old split to be lopsided, got {before:.0%}"
    assert after < 0.30, f"expected the new split to be even, got {after:.0%}"


def test_windows_still_cover_the_range_exactly():
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for parts in (1, 4, 7, 32):
        windows = sorted(split_windows(start, end, parts))
        assert windows[0][0] == start and windows[-1][1] == end
        for (_, a), (b, _) in zip(windows, windows[1:]):
            assert a == b, "no gaps and no overlap between windows"


def test_a_finely_split_scan_finds_exactly_what_a_coarse_one_does():
    graph, store = DemoGraph(delay=0), Store(":memory:")
    seed_demo_store(store)
    coarse = Scanner(graph, store, workers=1).scan("all")
    fine = Scanner(graph, store, workers=4).scan("all")
    assert coarse.checked == fine.checked
    assert {s["address"]: s["count"] for s in coarse.senders} == \
           {s["address"]: s["count"] for s in fine.senders}
