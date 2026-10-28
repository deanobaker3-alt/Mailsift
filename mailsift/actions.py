"""Carries out the choices made in the review screen.

Nothing is ever permanently deleted: "clear old" moves mail to Deleted Items,
and blocking moves it to Junk, so everything can be recovered in Outlook.
"""
from __future__ import annotations

from .classify import ALLOWED_ACTIONS
from .graph import GraphError
from .headers import parse_mailto
from .netguard import one_click_post

ACTIONS = {"keep", "unsub", "unsubdel", "block"}
RULE_PREFIX = "MailSift block: "


class Actions:
    def __init__(self, graph, store, dry_run=False, poster=one_click_post):
        self.graph = graph
        self.store = store
        self.dry_run = dry_run
        self.poster = poster

    def apply(self, choices: dict[str, str], scan, progress=None) -> list[dict]:
        progress = progress or (lambda **_: None)
        todo = [(sid, act) for sid, act in choices.items() if act != "keep"]
        results = []
        for done, (sid, action) in enumerate(todo, start=1):
            result = self._apply_one(sid, action, scan)
            results.append(result)
            progress(done=done, total=len(todo), result=result)
        return results

    # ------------------------------------------------------------------
    def _apply_one(self, sid, action, scan) -> dict:
        sender = scan.private.get(sid) if scan else None
        if sender is None:
            return {"id": sid, "name": "?", "address": "", "action": action, "status": "failed",
                    "outcome": "Not in the latest scan. Scan again and retry."}
        base = {"id": sid, "name": sender["name"], "address": sender["address"], "action": action}

        if action not in ACTIONS or action not in ALLOWED_ACTIONS.get(sender["kind"], {"keep"}):
            result = {**base, "status": "failed", "outcome": "Not allowed for this sender (looks dodgy: block it instead)"}
            self._log(sender, action, result)
            return result

        try:
            if action == "block":
                result = {**base, **self._block(sender)}
            else:
                result = {**base, **self._unsubscribe(sender)}
                if action == "unsubdel" and result["status"] != "failed":
                    result["outcome"] += self._move_note(sender, "deleteditems", "Deleted Items")
        except GraphError as exc:
            result = {**base, "status": "failed", "outcome": f"Microsoft Graph refused: {exc.message}"}
        self._log(sender, action, result)
        return result

    def _log(self, sender, action, result):
        self.store.log(sender["address"], sender["name"], action, result["status"],
                       result.get("outcome", ""), self.dry_run)

    # ------------------------------------------------------------------
    def _unsubscribe(self, sender) -> dict:
        address = sender["address"]
        notes = []

        if sender["kind"] == "oneclick" and sender["https"]:
            if self.dry_run:
                return {"status": "dry_run", "outcome": "Would send a one-click unsubscribe"}
            ok, detail = self.poster(sender["https"][0])
            if ok:
                self.store.mark_unsubscribed(address, "oneclick")
                return {"status": "done", "outcome": "Unsubscribed (one-click)"}
            notes.append(f"One-click failed: {detail}")

        for uri in sender["mailto"]:
            mail = parse_mailto(uri)
            if not mail:
                continue
            if self.dry_run:
                return {"status": "dry_run", "outcome": f"Would email an unsubscribe request to {mail['address']}"}
            self.graph.send_mail(mail["address"], mail["subject"], mail["body"])
            self.store.mark_unsubscribed(address, "mailto")
            return {"status": "done", "outcome": f"Unsubscribe request emailed to {mail['address']}"}

        links = sender["https"] + sender["http"]
        if links:
            if self.dry_run:
                return {"status": "dry_run", "outcome": "Would ask you to open the unsubscribe page"}
            self.store.mark_unsubscribed(address, "link")
            note = (" " + "; ".join(notes)) if notes else ""
            return {"status": "needs_you", "outcome": "Open the sender's unsubscribe page to finish." + note,
                    "link": links[0]}

        return {"status": "failed", "outcome": "; ".join(notes) or "No working unsubscribe option found"}

    def _move_note(self, sender, folder, label) -> str:
        if not sender["ids"]:
            return ""
        if self.dry_run:
            return f". Would move {len(sender['ids'])} old emails to {label}"
        destination = self.graph.well_known_folder_id(folder) or folder
        moved, failed = self.graph.move_messages(sender["ids"], destination)
        note = f". Moved {moved} old emails to {label}"
        return note + (f" ({failed} couldn't be moved)" if failed else "")

    def _block(self, sender) -> dict:
        address = sender["address"]
        if self.dry_run:
            return {"status": "dry_run",
                    "outcome": f"Would add a block rule and move {len(sender['ids'])} emails to Junk"}

        junk = self.graph.well_known_folder_id("junkemail") or "junkemail"
        display = (RULE_PREFIX + address)[:255]
        rule_id, rule_note = None, "Blocked: future emails go to Junk"
        try:
            rules = self.graph.list_rules()
            existing = next((r for r in rules if r.get("displayName") == display), None)
            if existing:
                rule_id = existing.get("id")
            else:
                sequence = max((r.get("sequence") or 0 for r in rules), default=0) + 1
                created = self.graph.create_rule({
                    "displayName": display,
                    "sequence": sequence,
                    "isEnabled": True,
                    "conditions": {"senderContains": [address]},
                    "actions": {"moveToFolder": junk, "stopProcessingRules": True},
                })
                rule_id = created.get("id")
        except GraphError as exc:
            rule_note = f"Couldn't create the block rule ({exc.message})"

        if rule_id:
            self.store.mark_blocked(address, rule_id)
        status = "done" if rule_id else "failed"
        return {"status": status, "outcome": rule_note + self._move_note(sender, "junkemail", "Junk")}
