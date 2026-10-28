"""A small Microsoft Graph client covering just what MailSift needs."""
from __future__ import annotations

import threading
import time
from urllib.parse import quote

import requests


class GraphError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Graph error {status}: {message}")
        self.status = status
        self.message = message


def _error_message(response) -> str:
    try:
        return response.json().get("error", {}).get("message") or response.text[:200]
    except ValueError:
        return response.text[:200]


def _mid(message_id: str) -> str:
    return quote(message_id, safe="")


class GraphClient:
    BASE = "https://graph.microsoft.com/v1.0"
    BATCH_SIZE = 20  # Graph's limit per $batch request

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
        return path if path.startswith("https://") else self.BASE + path

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
        """Runs requests through $batch, 20 at a time. Returns statuses in input order."""
        statuses = []
        for start in range(0, len(requests_list), self.BATCH_SIZE):
            chunk = requests_list[start:start + self.BATCH_SIZE]
            pending = {str(i): req for i, req in enumerate(chunk)}
            results: dict[str, int] = {}
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
                        results[rid] = status
                if not retry:
                    break
                pending = retry
                self.on_event("graph 429 inside batch: retrying in 5s")
                self.sleep(5)
            statuses.extend(results.get(str(i), 500) for i in range(len(chunk)))
        return statuses

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

    def get_message_headers(self, message_id):
        data = self.request("GET", f"/me/messages/{_mid(message_id)}", params={"$select": "internetMessageHeaders"})
        return data.get("internetMessageHeaders") or []

    def move_messages(self, message_ids, destination):
        """Moves messages (soft move, never a permanent delete). Returns (moved, failed)."""
        reqs = [{"method": "POST", "url": f"/me/messages/{_mid(mid)}/move", "body": {"destinationId": destination}}
                for mid in message_ids]
        statuses = self.batch(reqs) if reqs else []
        moved = sum(1 for s in statuses if 200 <= s < 300)
        return moved, len(statuses) - moved

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
