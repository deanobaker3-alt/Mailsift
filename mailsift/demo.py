"""Demo mode: a fake mailbox so you can try the whole flow without signing in.
Nothing real is touched; one-click unsubscribes are simulated."""
from __future__ import annotations

import random
import re
import time
from datetime import datetime, timedelta, timezone

from .graph import GraphError

FOLDERS = {"inbox": "f-inbox", "junkemail": "f-junk", "sentitems": "f-sent", "drafts": "f-drafts",
           "deleteditems": "f-deleted", "outbox": "f-outbox"}

PASS = "spf=pass smtp.mailfrom={d}; dkim=pass header.d={d};dmarc=pass action=none header.from={d};compauth=pass reason=100"
FAIL = "spf=fail smtp.mailfrom={d}; dkim=none (message not signed) header.d=none;dmarc=fail action=none header.from={d};compauth=fail reason=001"

# (name, address, emails, open %, days since last, unsubscribe style, auth, folder)
SENDERS = [
    ("Crust & Co Pizza", "offers@crustandco.co.uk", 214, 3, 1, "oneclick", PASS, "inbox"),
    ("FitFuel Supplements", "news@fitfuel.co.uk", 133, 1, 0, "mailto", PASS, "inbox"),
    ("VoltBox Electronics", "deals@voltbox.co.uk", 96, 0, 2, "oneclick", PASS, "inbox"),
    ("Bramble Home", "style@bramblehome.co.uk", 88, 2, 3, "oneclick", PASS, "inbox"),
    ("Wanderly Travel", "hello@wanderly.travel", 71, 8, 0, "oneclick", PASS, "inbox"),
    ("The Weekly Byte", "newsletter@weeklybyte.io", 52, 64, 1, "oneclick", PASS, "inbox"),
    ("GearHub Outdoor", "team@gearhub.co.uk", 40, 22, 7, "link", PASS, "inbox"),
    ("Kiln & Clay Studio", "studio@kilnandclay.co.uk", 18, 41, 14, "mailto", PASS, "inbox"),
    ("Amazon Rewards", "claim@amaz0n-prize.top", 12, 0, 0, "oneclick", FAIL, "junkemail"),
    ("Parcel Redelivery Notice", "track@parcel-redeliver.xyz", 9, 0, 0, "link", FAIL, "junkemail"),
    ("Snackbox Club", "hello@snackboxclub.co.uk", 14, 0, 1, "oneclick", PASS, "inbox"),
]
IGNORED_SENDER = "hello@snackboxclub.co.uk"   # pretend you unsubscribed 60 days ago and they kept going


def _headers(name, address, style, auth):
    domain = address.split("@", 1)[1]
    headers = [
        {"name": "Authentication-Results", "value": auth.format(d=domain)},
        {"name": "From", "value": f"{name} <{address}>"},
        {"name": "List-Id", "value": f"<news.{domain}>"},
    ]
    if style == "oneclick":
        headers += [
            {"name": "List-Unsubscribe", "value": f"<https://unsub.{domain}/u?id=demo>, <mailto:unsub@{domain}?subject=unsubscribe>"},
            {"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
        ]
    elif style == "mailto":
        headers.append({"name": "List-Unsubscribe", "value": f"<mailto:leave@{domain}?subject=unsubscribe>"})
    elif style == "link":
        headers.append({"name": "List-Unsubscribe", "value": f"<https://www.{domain}/preferences>"})
    return headers


class DemoGraph:
    def __init__(self, seed=7, delay=0.12):
        self.delay = delay
        rng = random.Random(seed)
        now = datetime.now(timezone.utc)
        self.messages = []
        self.sent = []
        self.rules = []
        self._moves = 0
        self._rule_ids = 0
        n = 0
        for name, address, count, open_pct, last_days, style, auth, folder in SENDERS:
            read_target = round(count * open_pct / 100)
            for i in range(count):
                n += 1
                age = last_days if i == 0 else rng.uniform(last_days, 360)
                self.messages.append({
                    "id": f"demo-{n}",
                    "from": {"emailAddress": {"name": name, "address": address}},
                    "receivedDateTime": (now - timedelta(days=age)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "isRead": i < read_target,
                    "parentFolderId": FOLDERS[folder],
                    "internetMessageHeaders": _headers(name, address, style, auth),
                })
        # Personal email: no list headers, so it must never show up.
        for i in range(30):
            n += 1
            self.messages.append({
                "id": f"demo-{n}",
                "from": {"emailAddress": {"name": "Mum", "address": "mum@example.co.uk"}},
                "receivedDateTime": (now - timedelta(days=i * 5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "isRead": True, "parentFolderId": FOLDERS["inbox"],
                "internetMessageHeaders": [{"name": "From", "value": "Mum <mum@example.co.uk>"}],
            })
        self.messages.sort(key=lambda m: m["receivedDateTime"], reverse=True)

    # -- the GraphClient interface --------------------------------------
    def me(self):
        return {"displayName": "Demo User", "userPrincipalName": "demo@hotmail.co.uk"}

    def well_known_folder_id(self, name):
        return FOLDERS.get(name)

    def _filtered(self, filter_expr):
        out = list(self.messages)
        for op, value in re.findall(r"receivedDateTime (ge|lt) (\S+)", filter_expr or ""):
            out = [m for m in out if (m["receivedDateTime"] >= value if op == "ge" else m["receivedDateTime"] < value)]
        return out

    def count_messages(self, filter_expr=None):
        return len(self._filtered(filter_expr))

    def oldest_message_time(self):
        return min((m["receivedDateTime"] for m in self.messages), default=None)

    def list_messages(self, params):
        for i, message in enumerate(self._filtered(params.get("$filter"))):
            if i % 50 == 0:
                time.sleep(self.delay)  # feel like a real scan
            yield dict(message)

    def get_message_headers(self, message_id):
        return next((m["internetMessageHeaders"] for m in self.messages if m["id"] == message_id), [])

    def move_messages(self, message_ids, destination):
        # Like Outlook, a moved message gets a new id.
        ids = set(message_ids)
        moved = []
        for m in self.messages:
            if m["id"] in ids:
                self._moves += 1
                new_id = f"{m['id'].split('~')[0]}~{self._moves}"
                moved.append((m["id"], new_id))
                m["id"], m["parentFolderId"] = new_id, destination
        return moved, len(ids) - len(moved)

    def send_mail(self, to, subject, body):
        self.sent.append({"to": to, "subject": subject, "body": body})

    def list_rules(self):
        return list(self.rules)

    def create_rule(self, rule):
        if any(r["displayName"] == rule["displayName"] for r in self.rules):
            raise GraphError(400, "Duplicate rule")
        self._rule_ids += 1
        created = {**rule, "id": f"rule-{self._rule_ids}"}
        self.rules.append(created)
        return created

    def update_rule(self, rule_id, changes):
        rule = next(r for r in self.rules if r["id"] == rule_id)
        rule.update(changes)
        return rule

    def delete_rule(self, rule_id):
        self.rules = [r for r in self.rules if r["id"] != rule_id]


class DemoAuth:
    cache_encrypted = True

    def __init__(self):
        self.flow = {"state": "idle"}
        self._signed_in = True

    def username(self):
        return "demo@hotmail.co.uk" if self._signed_in else None

    def token(self):
        return "demo-token"

    def signed_in(self):
        return self._signed_in

    def start_interactive(self):
        self._signed_in = True
        self.flow = {"state": "done"}
        return self.flow

    def logout(self):
        self._signed_in = False

    forget = logout


def demo_poster(url):
    time.sleep(0.15)
    return True, "HTTP 202 (demo, nothing sent)"


def seed_demo_store(store):
    then = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.mark_unsubscribed(IGNORED_SENDER, "oneclick", at=then)
