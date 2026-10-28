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
from .headers import (first, has_one_click, header_map, parse_auth_results,
                      parse_list_unsubscribe, split_unsubscribe_uris)

SCOPES = {"12m": 365, "2y": 730, "all": None}
SKIP_FOLDERS = ("sentitems", "drafts", "deleteditems", "outbox")
IGNORE_GRACE = timedelta(days=3)  # senders get a few days to act on an unsubscribe
SELECT = "id,from,receivedDateTime,isRead,parentFolderId,internetMessageHeaders"
WORKERS = 4                       # Outlook allows 4 concurrent requests per mailbox per app
PAGE_SIZE = 50                    # headers are chunky; bigger pages risk gateway timeouts
FMT = "%Y-%m-%dT%H:%M:%SZ"


def sender_id(address: str) -> str:
    return hashlib.sha256(address.encode()).hexdigest()[:12]


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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


class Scanner:
    def __init__(self, graph, store, page_size=PAGE_SIZE, workers=WORKERS, now=None):
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

        state = {
            "groups": {}, "checked": 0, "lock": threading.Lock(),
            "unsubscribed": self.store.unsubscribed(), "skip": skip, "junk": junk_id,
        }
        progress(checked=0, found=0, total=total)

        windows = split_windows(start, end, self.workers)

        def read_window(index, window):
            a, b = window
            params = {
                "$select": SELECT, "$top": str(self.page_size), "$orderby": "receivedDateTime desc",
                "$filter": f"receivedDateTime ge {a.strftime(FMT)} and receivedDateTime lt {b.strftime(FMT)}",
            }
            log(f"worker {index + 1}: {a:%b %Y} – {b:%b %Y}")
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
            log(f"worker {index + 1}: done, {seen:,} emails")

        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="scan") as pool:
            futures = [pool.submit(read_window, i, w) for i, w in enumerate(windows)]
            for future in futures:
                future.result()  # re-raises the first worker error, if any

        progress(checked=state["checked"], found=len(state["groups"]), total=total)
        result = self._build(scope, state["checked"], state["groups"], state["unsubscribed"],
                             self.store.blocked(), self.store.protected())
        result.cancelled = cancelled()
        return result

    # ------------------------------------------------------------------
    def _ingest(self, msg, state):
        if msg.get("parentFolderId") in state["skip"]:
            return
        headers = msg.get("internetMessageHeaders")
        if headers is None:  # some mailboxes don't return headers in list calls
            headers = self.graph.get_message_headers(msg["id"])
        hmap = header_map(headers)
        list_unsub = first(hmap, "list-unsubscribe")
        if not list_unsub and not first(hmap, "list-id"):
            return  # not mailing-list mail: personal email is never touched

        email = (msg.get("from") or {}).get("emailAddress") or {}
        address = (email.get("address") or "").strip().lower()
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
                    "auth": parse_auth_results(hmap.get("authentication-results")), "auth_at": when,
                    "unsub_at": None, "one_click": False, "https": [], "http": [], "mailto": [],
                }
            # Workers finish in any order, so always prefer details from the newest email.
            if when >= g["auth_at"]:
                g["auth"], g["auth_at"] = parse_auth_results(hmap.get("authentication-results")), when
            if list_unsub and (g["unsub_at"] is None or when >= g["unsub_at"]):
                g["https"], g["http"], g["mailto"] = split_unsubscribe_uris(parse_list_unsubscribe(list_unsub))
                g["one_click"] = has_one_click(first(hmap, "list-unsubscribe-post"))
                g["unsub_at"] = when

            g["count"] += 1
            g["read"] += 1 if msg.get("isRead") else 0
            g["junk"] += 1 if state["junk"] and msg.get("parentFolderId") == state["junk"] else 0
            g["ids"].append(msg["id"])
            g["folders"].append(msg.get("parentFolderId") or "inbox")
            g["names"][(email.get("name") or "").strip()] += 1
            g["newest"] = max(g["newest"], when)
            g["oldest"] = min(g["oldest"], when)

            unsub_at = state["unsubscribed"].get(address)
            if unsub_at and when > parse_time(unsub_at) + IGNORE_GRACE:
                g["after_unsub"] += 1

    def _build(self, scope, checked, groups, unsubscribed, blocked, protected=frozenset()) -> ScanResult:
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
            })
            private[sid] = {
                "address": address, "name": name, "kind": verdict["kind"],
                "ids": list(g["ids"]), "folders": list(g["folders"]),
                "https": g["https"], "http": g["http"], "mailto": g["mailto"],
            }
        senders.sort(key=lambda s: (-s["count"], s["address"]))
        return ScanResult(scope=scope, checked=checked, senders=senders, private=private)
