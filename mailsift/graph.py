"""A small Microsoft Graph client covering just what MailSift needs."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import requests


class GraphError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Graph error {status}: {message}")
        self.status = status
        self.message = message


def _error_message(response) -> str:
    """Graph's error text, or the raw body. Never raises: an odd-shaped error
    reply must not turn a GraphError into some unrelated exception."""
    fallback = (getattr(response, "text", "") or "")[:200]
    try:
        payload = response.json()
    except ValueError:
        return fallback
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        return message if isinstance(message, str) and message else fallback
    return error if isinstance(error, str) and error else fallback


def _mid(message_id: str) -> str:
    return quote(message_id, safe="")


class GraphClient:
    BASE = "https://graph.microsoft.com/v1.0"
    BATCH_SIZE = 20  # Graph's limit per $batch request
    CONCURRENCY = 4  # Outlook's per-mailbox ceiling; more just earns 429s

    def __init__(self, token_provider, session_factory=requests.Session, sleep=time.sleep, on_event=None):
        self.token_provider = token_provider
        self.session_factory = session_factory
        self.sleep = sleep
        self.on_event = on_event or (lambda message: None)
        self._local = threading.local()     # one HTTP session per thread: requests.Session isn't thread-safe
        self._folders: dict[str, str | None] = {}

    @property
    def session(self):
        if not hasattr(self._local, "session"):
            self._local.session = self.session_factory()
        return self._local.session

    # -- plumbing --------------------------------------------------------
    def _url(self, path: str) -> str:
        if path.startswith("/"):
            return self.BASE + path
        # Absolute URLs only come from Graph's own @odata.nextLink. Refuse
        # anything else so the access token can never be sent to another host.
        if path.startswith(self.BASE + "/"):
            return path
        raise GraphError(0, "Refusing to send credentials to a non-Graph URL")

    def request(self, method, path, params=None, json=None, headers=None):
        for attempt in range(6):
            all_headers = {"Authorization": f"Bearer {self.token_provider()}"}
            all_headers.update(headers or {})
            response = self.session.request(
                method, self._url(path), params=params, json=json, headers=all_headers, timeout=60
            )
            if response.status_code in (429, 503, 504) and attempt < 5:
                # Throttled or busy: wait as long as Graph asks, else back off exponentially.
                retry_after = response.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else min(2 ** attempt, 30)
                self.on_event(f"graph {response.status_code}: backing off {max(wait, 1)}s")
                self.sleep(max(wait, 1))
                continue
            if response.status_code >= 400:
                raise GraphError(response.status_code, _error_message(response))
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        raise GraphError(429, "Still throttled after several retries")

    def get_all(self, path, params=None):
        url, query = path, params
        while url:
            data = self.request("GET", url, params=query)
            yield from data.get("value", [])
            url, query = data.get("@odata.nextLink"), None  # nextLink already carries the query

    def batch(self, requests_list):
        """Runs requests through $batch, 20 at a time. Returns (status, body) pairs in input order.

        The chunks go out concurrently. Tidying a single busy sender can mean
        thousands of moves, which is a hundred chunks, and sending those one
        after another made a clean-up take minutes of pure waiting.
        """
        chunks = [requests_list[i:i + self.BATCH_SIZE]
                  for i in range(0, len(requests_list), self.BATCH_SIZE)]
        if not chunks:
            return []
        if len(chunks) == 1:
            return self._batch_chunk(chunks[0])
        # Same ceiling the scan uses: Outlook allows only a few requests at
        # once per mailbox, and going over earns 429s and a slower result.
        workers = min(self.CONCURRENCY, len(chunks))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="batch") as pool:
            done = list(pool.map(self._batch_chunk, chunks))   # map keeps input order
        return [pair for chunk in done for pair in chunk]

    def _batch_chunk(self, chunk):
        pending = {str(i): req for i, req in enumerate(chunk)}
        results: dict[str, tuple] = {}
        for attempt in range(3):
            body = {"requests": [
                {"id": rid, "method": req["method"], "url": req["url"],
                 "body": req.get("body"), "headers": {"Content-Type": "application/json"}}
                for rid, req in pending.items()
            ]}
            data = self.request("POST", "/$batch", json=body)
            retry = {}
            for resp in data.get("responses", []):
                rid, status = resp.get("id"), resp.get("status", 500)
                if status == 429 and attempt < 2:
                    retry[rid] = pending[rid]
                else:
                    results[rid] = (status, resp.get("body") or {})
            if not retry:
                break
            pending = retry
            self.on_event("graph 429 inside batch: retrying in 5s")
            self.sleep(5)
        return [results.get(str(i), (500, {})) for i in range(len(chunk))]

    # -- mailbox ---------------------------------------------------------
    def me(self):
        return self.request("GET", "/me", params={"$select": "displayName,mail,userPrincipalName"})

    def well_known_folder_id(self, name: str):
        if name not in self._folders:
            try:
                self._folders[name] = self.request("GET", f"/me/mailFolders/{name}", params={"$select": "id"})["id"]
            except (GraphError, KeyError):
                self._folders[name] = None
        return self._folders[name]

    def count_messages(self, filter_expr=None):
        params = {"$count": "true", "$top": "1", "$select": "id"}
        if filter_expr:
            params["$filter"] = filter_expr
        try:
            return self.request("GET", "/me/messages", params=params, headers={"ConsistencyLevel": "eventual"}).get("@odata.count")
        except GraphError:
            return None

    def oldest_message_time(self):
        """receivedDateTime of the oldest message in the mailbox, or None if it's empty."""
        data = self.request("GET", "/me/messages", params={
            "$select": "receivedDateTime", "$orderby": "receivedDateTime asc", "$top": "1"})
        items = data.get("value", [])
        return items[0].get("receivedDateTime") if items else None

    def list_messages(self, params):
        return self.get_all("/me/messages", params)

    # -- asking for four headers instead of all of them ------------------
    # MailSift reads exactly four headers, but internetMessageHeaders brings
    # back every one: Received chains, DKIM signatures, the lot, several
    # kilobytes a message. Outlook can return named headers individually as
    # MAPI properties, which is a few hundred bytes instead.
    WANTED_HEADERS = ("List-Unsubscribe", "List-Unsubscribe-Post", "List-Id", "Authentication-Results")
    _HEADER_NAMESPACE = "{00020386-0000-0000-C000-000000000046}"

    @classmethod
    def header_expand(cls) -> str:
        ids = " or ".join(f"id eq 'String {cls._HEADER_NAMESPACE} Name {name}'"
                          for name in cls.WANTED_HEADERS)
        return f"singleValueExtendedProperties($filter={ids})"

    @staticmethod
    def headers_from_properties(msg) -> list[dict]:
        """The same [{name, value}] shape internetMessageHeaders gives."""
        out = []
        for prop in msg.get("singleValueExtendedProperties") or []:
            pid = prop.get("id") or ""
            if " Name " in pid:
                out.append({"name": pid.split(" Name ", 1)[1], "value": prop.get("value") or ""})
        return out

    def cheap_headers_work(self, since: str) -> bool:
        """Proves on this mailbox that the cheap request gives the same answer.

        Asked for in exactly the shape the scan uses, because it's combining
        $expand with $filter and $orderby that Outlook is most likely to
        refuse. Both kinds of header come back in the one reply and are
        compared against each other, so a wrong property id shows up as a
        mismatch rather than as a scan that quietly finds nothing.
        """
        try:
            data = self.request("GET", "/me/messages", params={
                "$select": "id,internetMessageHeaders",
                "$expand": self.header_expand(),
                "$top": "60",
                "$orderby": "receivedDateTime desc",
                "$filter": f"receivedDateTime ge {since}",
            })
        except GraphError as exc:
            self.on_event(f"headers: Outlook won't send them individually ({exc.status}), asking for all of them")
            return False

        wanted = {name.lower() for name in self.WANTED_HEADERS}
        compared = 0
        for msg in data.get("value", []):
            full = {}
            for item in msg.get("internetMessageHeaders") or []:
                name = (item.get("name") or "").lower()
                if name in wanted:
                    full.setdefault(name, item.get("value") or "")
            cheap = {}
            for item in self.headers_from_properties(msg):
                cheap.setdefault((item["name"] or "").lower(), item["value"])
            if not full:
                continue          # nothing to compare on this one
            compared += 1
            if {k: v.strip() for k, v in full.items()} != {k: v.strip() for k, v in cheap.items()}:
                self.on_event("headers: the short form didn't match, asking for all of them")
                return False
        if not compared:
            # No recent message carries any of them, so there's no evidence
            # either way. Take the slow, certain route.
            return False
        self.on_event(f"headers: asking for only the {len(wanted)} MailSift reads ({compared} checked)")
        return True

    def get_message_headers(self, message_id):
        data = self.request("GET", f"/me/messages/{_mid(message_id)}", params={"$select": "internetMessageHeaders"})
        return data.get("internetMessageHeaders") or []

    def move_messages(self, message_ids, destination):
        """Moves messages (soft move, never a permanent delete).

        Outlook gives a message a new id when it changes folder, so this returns
        ([(old_id, new_id), ...], failed_count) to let the move be undone later.
        """
        message_ids = list(message_ids)
        reqs = [{"method": "POST", "url": f"/me/messages/{_mid(mid)}/move", "body": {"destinationId": destination}}
                for mid in message_ids]
        results = self.batch(reqs) if reqs else []
        moved = [(old, body.get("id") or old) for old, (status, body) in zip(message_ids, results) if 200 <= status < 300]
        return moved, len(results) - len(moved)

    def sent_recipients(self, limit=2000) -> set[str]:
        """Lower-cased addresses you've written to, from your newest `limit`
        sent emails. Recipients only: no subjects, no bodies. Held in memory
        for the scan and never stored."""
        params = {"$select": "toRecipients,ccRecipients,bccRecipients",
                  "$orderby": "sentDateTime desc", "$top": "200"}
        found, seen = set(), 0
        for msg in self.get_all("/me/mailFolders/sentitems/messages", params):
            for field in ("toRecipients", "ccRecipients", "bccRecipients"):
                for recipient in msg.get(field) or []:
                    address = ((recipient or {}).get("emailAddress") or {}).get("address")
                    if isinstance(address, str) and "@" in address:
                        found.add(address.strip().lower())
            seen += 1
            if seen >= limit:
                break
        return found

    def send_mail(self, to, subject, body):
        self.request("POST", "/me/sendMail", json={
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [{"emailAddress": {"address": to}}],
            },
            "saveToSentItems": True,
        })

    def list_rules(self):
        return list(self.get_all("/me/mailFolders/inbox/messageRules"))

    def create_rule(self, rule):
        return self.request("POST", "/me/mailFolders/inbox/messageRules", json=rule)

    def update_rule(self, rule_id, changes):
        return self.request("PATCH", f"/me/mailFolders/inbox/messageRules/{quote(rule_id, safe='')}", json=changes)

    def delete_rule(self, rule_id):
        self.request("DELETE", f"/me/mailFolders/inbox/messageRules/{quote(rule_id, safe='')}")
