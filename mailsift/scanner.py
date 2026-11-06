"""Walks the mailbox, finds mailing-list mail, and groups it by sender.

The scan range is split into date windows that are read in parallel (Graph
allows a handful of concurrent requests per mailbox), which makes big
mailboxes several times quicker than reading page after page.
"""
from __future__ import annotations

import hashlib
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .classify import classify
from .company import company_of
from .score import RECENT_WINDOW, assess
from .headers import (first, has_one_click, header_map, normalise_address,
                      parse_auth_results, parse_list_unsubscribe, split_unsubscribe_uris)

SCOPES = {"12m": 365, "2y": 730, "all": None}
SKIP_FOLDERS = ("sentitems", "drafts", "deleteditems", "outbox")
IGNORE_GRACE = timedelta(days=3)  # senders get a few days to act on an unsubscribe
SELECT = "id,from,receivedDateTime,isRead,parentFolderId,internetMessageHeaders"
# The same, minus the headers: used when Outlook will send just the four
# MailSift reads, which is most of the payload saved.
SELECT_LIGHT = "id,from,receivedDateTime,isRead,parentFolderId"
WORKERS = 4                       # Outlook allows 4 concurrent requests per mailbox per app
SENT_LIMIT = 2000                 # newest sent emails checked for "have you written to them"
FMT = "%Y-%m-%dT%H:%M:%SZ"


def sender_id(address: str) -> str:
    return hashlib.sha256(address.encode()).hexdigest()[:12]


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def window_count(total, workers: int) -> int:
    """How many date windows to cut the range into.

    One window per worker sounds right and isn't: windows of equal *time*
    hold wildly unequal amounts of mail, because a mailbox gets busier as it
    ages. Measured on a realistic two-year mailbox, the newest quarter held
    53% of everything, so three workers finished early and the scan waited on
    the fourth. Cutting the range finer than the worker count lets the pool
    even itself out whatever the shape of the mailbox, and costs only one
    extra request per window.
    """
    if not total:
        return workers          # size unknown: don't pay for windows that may be empty
    return max(workers, min(workers * 8, -(-int(total) // 500)))


def split_windows(start: datetime, end: datetime, parts: int):
    """[start, end) cut into `parts` equal date windows, newest first."""
    step = (end - start) / parts
    edges = [start + step * i for i in range(parts)] + [end]
    windows = [(edges[i], edges[i + 1]) for i in range(parts)]
    return list(reversed(windows))


@dataclass
class ScanResult:
    scope: str
    checked: int
    senders: list[dict]                                      # safe to send to the browser
    private: dict[str, dict] = field(default_factory=dict)  # message ids + unsubscribe URIs, server side only
    cancelled: bool = False
    other_senders: int = 0    # senders seen but not listed: not mailing-list mail
    other_messages: int = 0
    sent_checked: bool = False   # whether Sent Items could be read this time


class Scanner:
    def __init__(self, graph, store, page_size=None, workers=WORKERS, now=None):
        self.graph = graph
        self.store = store
        self.page_size = page_size
        self.workers = workers
        self.now = now or (lambda: datetime.now(timezone.utc))

    def scan(self, scope="12m", progress=None, cancelled=None, log=None) -> ScanResult:
        progress = progress or (lambda **_: None)
        cancelled = cancelled or (lambda: False)
        log = log or (lambda message: None)
        if scope not in SCOPES:
            raise ValueError("Unknown scan range")

        skip = {fid for fid in (self.graph.well_known_folder_id(n) for n in SKIP_FOLDERS) if fid}
        junk_id = self.graph.well_known_folder_id("junkemail")

        end = self.now() + timedelta(minutes=5)
        days = SCOPES[scope]
        if days:
            start = self.now() - timedelta(days=days)
        else:
            oldest = self.graph.oldest_message_time()
            if not oldest:
                return ScanResult(scope=scope, checked=0, senders=[])
            start = parse_time(oldest) - timedelta(seconds=1)
        log(f"range {start:%d %b %Y} → today")

        total = self.graph.count_messages(f"receivedDateTime ge {start.strftime(FMT)}")
        if total is not None:
            log(f"mailbox reports {total:,} emails in range")

        # Asked of this mailbox, once, before any window is read: how it
        # will let the scan request messages decides most of the time taken.
        cheap, page_size, _why = self.graph.probe_scan_query(start.strftime(FMT))
        if self.page_size is not None:
            page_size = self.page_size          # pinned by a caller or a test

        now = self.now()
        state = {
            "cheap": cheap,
            "groups": {}, "checked": 0, "lock": threading.Lock(),
            "unsubscribed": self.store.unsubscribed(), "skip": skip, "junk": junk_id,
            "other_senders": set(), "other_count": 0,
            "recent_since": now - RECENT_WINDOW,
        }
        progress(checked=0, found=0, total=total)

        windows = split_windows(start, end, window_count(total, self.workers))
        if len(windows) > self.workers:
            log(f"{len(windows)} date windows across {self.workers} workers")

        def read_window(index, window):
            a, b = window
            params = {
                "$select": SELECT_LIGHT if cheap else SELECT,
                "$top": str(page_size), "$orderby": "receivedDateTime desc",
                "$filter": f"receivedDateTime ge {a.strftime(FMT)} and receivedDateTime lt {b.strftime(FMT)}",
            }
            if cheap:
                params["$expand"] = self.graph.header_expand()
            log(f"window {index + 1}: {a:%b %Y} – {b:%b %Y}")
            seen = 0
            for msg in self.graph.list_messages(params):
                if cancelled():
                    return
                self._ingest(msg, state)
                seen += 1
                with state["lock"]:
                    state["checked"] += 1
                    checked, found = state["checked"], len(state["groups"])
                if checked % 25 == 0:
                    progress(checked=checked, found=found, total=total)
            log(f"window {index + 1}: done, {seen:,} emails")

        sent = {"addresses": None}

        def read_sent():
            # Who you've written to: the strongest "this is wanted" signal
            # there is. Optional by design. If Sent Items can't be read, the
            # scan carries on and the score just goes without it.
            fetch = getattr(self.graph, "sent_recipients", None)
            if fetch is None or cancelled():
                return
            try:
                sent["addresses"] = fetch(SENT_LIMIT)
                log(f"sent items: checked who you've written to ({len(sent['addresses']):,} addresses)")
            except Exception as exc:  # never let this take the scan down
                log(f"sent items: couldn't read them ({exc.__class__.__name__}), carrying on without")

        # Same pool, so it never adds to Graph's per-mailbox concurrency: it
        # simply runs when the first date window finishes.
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="scan") as pool:
            futures = [pool.submit(read_window, i, w) for i, w in enumerate(windows)]
            sent_future = pool.submit(read_sent)
            for future in futures:
                future.result()  # re-raises the first worker error, if any
            sent_future.result()

        progress(checked=state["checked"], found=len(state["groups"]), total=total)
        result = self._build(scope, state["checked"], state["groups"], state["unsubscribed"],
                             self.store.blocked(), self.store.protected(),
                             emailed=sent["addresses"], now=now)
        result.cancelled = cancelled()
        result.other_senders = len(state["other_senders"])
        result.other_messages = state["other_count"]
        result.sent_checked = sent["addresses"] is not None
        return result

    # ------------------------------------------------------------------
    def _ingest(self, msg, state):
        if msg.get("parentFolderId") in state["skip"]:
            return
        headers = msg.get("internetMessageHeaders")
        if headers is None:
            if state["cheap"]:
                # Outlook leaves the field out entirely when a message has
                # none of them, which means "no such headers", not "unknown".
                headers = self.graph.headers_from_properties(msg)
            else:  # some mailboxes don't return headers in list calls at all
                headers = self.graph.get_message_headers(msg["id"])
        hmap = header_map(headers)
        list_unsub = first(hmap, "list-unsubscribe")
        auth = parse_auth_results(hmap.get("authentication-results"))

        email = (msg.get("from") or {}).get("emailAddress") or {}
        address = normalise_address(email.get("address"))

        if not list_unsub and not first(hmap, "list-id"):
            # Not mailing-list mail, so it's never listed or touched. Counted
            # anyway: without a number on screen, someone looking for a sender
            # that doesn't set these headers just sees an absence and assumes
            # the scan is broken.
            if address:
                with state["lock"]:
                    state["other_senders"].add(address)
                    state["other_count"] += 1
            return

        received = msg.get("receivedDateTime") or ""
        if not address or not received:
            return
        when = parse_time(received)

        with state["lock"]:
            g = state["groups"].get(address)
            if g is None:
                g = state["groups"][address] = {
                    "address": address, "names": Counter(), "count": 0, "read": 0, "junk": 0,
                    "newest": when, "oldest": when, "ids": [], "folders": [], "after_unsub": 0,
                    "auth": auth, "auth_at": when,
                    "unsub_at": None, "one_click": False, "https": [], "http": [], "mailto": [],
                    "recent": 0, "recent_read": 0, "last_read": None,
                }
            # Workers finish in any order, so always prefer details from the newest email.
            if when >= g["auth_at"]:
                g["auth"], g["auth_at"] = auth, when
            if list_unsub and (g["unsub_at"] is None or when >= g["unsub_at"]):
                g["https"], g["http"], g["mailto"] = split_unsubscribe_uris(parse_list_unsubscribe(list_unsub))
                g["one_click"] = has_one_click(first(hmap, "list-unsubscribe-post"))
                g["unsub_at"] = when

            is_read = bool(msg.get("isRead"))
            g["count"] += 1
            g["read"] += 1 if is_read else 0
            # Engagement trend: the last few months against everything before.
            if when >= state["recent_since"]:
                g["recent"] += 1
                g["recent_read"] += 1 if is_read else 0
            if is_read and (g["last_read"] is None or when > g["last_read"]):
                g["last_read"] = when
            g["junk"] += 1 if state["junk"] and msg.get("parentFolderId") == state["junk"] else 0
            g["ids"].append(msg["id"])
            g["folders"].append(msg.get("parentFolderId") or "inbox")
            g["names"][(email.get("name") or "").strip()] += 1
            g["newest"] = max(g["newest"], when)
            g["oldest"] = min(g["oldest"], when)

            unsub_at = state["unsubscribed"].get(address)
            if unsub_at and when > parse_time(unsub_at) + IGNORE_GRACE:
                g["after_unsub"] += 1

    def _build(self, scope, checked, groups, unsubscribed, blocked, protected=frozenset(),
               emailed=None, now=None) -> ScanResult:
        now = now or self.now()
        senders, private = [], {}
        for address, g in groups.items():
            name = next((n for n, _ in g["names"].most_common() if n), "") or address
            verdict = classify(
                address=address, display_name=name, auth=g["auth"], one_click=g["one_click"],
                https=g["https"], http=g["http"], mailto=g["mailto"],
                junk_count=g["junk"], count=g["count"],
            )
            span_days = max(30.0, (g["newest"] - g["oldest"]).total_seconds() / 86400)
            sid = sender_id(address)
            senders.append({
                "id": sid,
                "name": name,
                "address": address,
                "domain": address.rsplit("@", 1)[-1],
                "count": g["count"],
                "read": g["read"],
                "open_pct": round(100 * g["read"] / g["count"]) if g["count"] else 0,
                "per_month": round(g["count"] / (span_days / 30.44), 1),
                "last": g["newest"].strftime(FMT),
                "first": g["oldest"].strftime(FMT),
                "kind": verdict["kind"],
                "reasons": verdict["reasons"],
                "auth": {k: g["auth"].get(k) for k in ("spf", "dkim", "dmarc")},
                "ignored": g["after_unsub"] > 0,
                "ignored_count": g["after_unsub"],
                "unsubscribed_at": unsubscribed.get(address),
                "blocked": address in blocked,
                "protected": address in protected,
                "junk": g["junk"],
                "recent_count": g["recent"],
                "recent_opened": g["recent_read"],
                "older_count": g["count"] - g["recent"],
                "older_opened": g["read"] - g["recent_read"],
                # When the newest email you opened arrived (not when you read it:
                # Outlook doesn't say).
                "last_opened": g["last_read"].strftime(FMT) if g["last_read"] else None,
                # None means "couldn't check", which is not the same as "no".
                "emailed": (address in emailed) if emailed is not None else None,
            })
            # assess() needs the unsubscribe addresses to judge whether
            # emailing them is reasonable; they stay server-side afterwards.
            senders[-1].update(assess({**senders[-1], "mailto": g["mailto"]}, now))
            senders[-1].update(company_of(address, name))
            private[sid] = {
                "address": address, "name": name, "kind": verdict["kind"],
                # Signals the apply step needs to decide whether MailSift may
                # email an unsubscribe on the user's behalf. Kept server-side:
                # the browser never gets to choose this.
                "read": g["read"], "count": g["count"], "junk": g["junk"],
                "score": senders[-1]["score"], "tier": senders[-1]["tier"],
                "emailed": senders[-1]["emailed"],
                "ids": g["ids"], "folders": g["folders"],
                "https": g["https"], "http": g["http"], "mailto": g["mailto"],
            }
        senders.sort(key=lambda s: (-s["count"], s["address"]))
        return ScanResult(scope=scope, checked=checked, senders=senders, private=private)
