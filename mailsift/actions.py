"""Carries out the choices made in the review screen, and undoes them.

Nothing is ever permanently deleted: "purge" moves mail to Deleted Items and
blocking moves it to Junk. Every live clean-up is recorded as a "run" (which
messages moved, from which folder, and who was blocked) so it can be undone.
Unsubscribes can't be undone: that request has already gone to the sender.
"""
from __future__ import annotations

from collections import defaultdict

import logging

from .blocklist import Blocklist
from .classify import ALLOWED_ACTIONS, registrable_domain
from .company import PLATFORMS
from .graph import GraphError
from .headers import looks_like_unsubscribe_mailbox, parse_mailto
from .netguard import one_click_post

log = logging.getLogger("mailsift")

ACTIONS = {"keep", "unsub", "unsubdel", "block"}


class Actions:
    def __init__(self, graph, store, dry_run=False, poster=one_click_post):
        self.graph = graph
        self.store = store
        self.dry_run = dry_run
        self.poster = poster
        self.blocklist = Blocklist(graph)
        self.run_id = None

    # ------------------------------------------------------------------ apply
    def apply(self, choices: dict[str, str], scan, progress=None) -> list[dict]:
        progress = progress or (lambda **_: None)
        todo = [(sid, act) for sid, act in choices.items() if act != "keep"]
        self.run_id = None
        protected = self.store.protected()
        results, pending_blocks = [], []
        for done, (sid, action) in enumerate(todo, start=1):
            # Each sender stands alone. A timeout on the third of twenty used
            # to abort the rest of the run, leaving the user with a generic
            # error and no idea what had been done.
            try:
                result = self._apply_one(sid, action, scan, protected)
            except Exception as exc:
                log.exception("Clean-up failed for %s", sid)
                result = {"id": sid, "name": "?", "address": "", "action": action, "status": "failed",
                          "outcome": f"Something went wrong ({exc.__class__.__name__}); "
                                     f"the other senders were still done. Try this one again."}
            if result.get("status") == "pending":
                pending_blocks.append(result)
            results.append(result)
            progress(done=done, total=len(todo), result=result)
        if pending_blocks:
            self._finish_blocks(pending_blocks)
        return results

    def _run_id(self):
        """The undo record for this clean-up, created only once something
        worth undoing happens. Every clean-up used to start one, so a run of
        plain unsubscribes left a row behind that could never be undone."""
        if self.dry_run:
            return None
        if self.run_id is None:
            self.run_id = self.store.start_run()
        return self.run_id

    def _apply_one(self, sid, action, scan, protected) -> dict:
        sender = scan.private.get(sid) if scan else None
        if sender is None:
            return {"id": sid, "name": "?", "address": "", "action": action, "status": "failed",
                    "outcome": "Not in the latest scan. Scan again and retry."}
        base = {"id": sid, "name": sender["name"], "address": sender["address"], "action": action}

        if sender["address"] in protected:
            result = {**base, "status": "failed", "outcome": "On your never-touch list, so left alone"}
            self._log(sender, action, result)
            return result
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
        if result["status"] != "pending":
            self._log(sender, action, result)
        return result

    def _log(self, sender, action, result):
        self.store.log(sender["address"], sender["name"], action, result["status"],
                       result.get("outcome", ""), self.dry_run)

    # ------------------------------------------------------------ unsubscribe
    @staticmethod
    def may_email_unsubscribe(sender, to: str) -> bool:
        """May MailSift send an unsubscribe email to this address?

        Emailing is the one action with consequences outside this computer:
        it sends from your account and tells the recipient a human reads it.
        Authentication alone is not enough to earn that. A spammer who owns
        their domain and configures DNS properly passes it, and MailSift was
        duly emailing people like vonni.n@<throwaway domain> — exactly the
        "never unsubscribe from spam" rule it exists to enforce.

        So this needs a positive reason to believe the recipient is a real
        unsubscribe system, not just the absence of a reason to doubt it.
        Any one of these will do:
        """
        if not to:
            return False
        # ...the address looks like a list's unsubscribe mailbox, not a person
        if looks_like_unsubscribe_mailbox(to):
            return True
        # ...it's an email platform's own domain, shared by all its customers
        if registrable_domain(to.rsplit("@", 1)[-1]) in PLATFORMS:
            return True
        # ...you've written to them, so you already correspond
        if sender.get("emailed"):
            return True
        # ...you've opened their mail, so it's wanted post badly addressed
        return bool(sender.get("read"))

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

        refused = None
        for uri in sender["mailto"]:
            mail = parse_mailto(uri)
            if not mail:
                continue
            if not self.may_email_unsubscribe(sender, mail["address"]):
                refused = mail["address"]
                continue
            if self.dry_run:
                return {"status": "dry_run", "outcome": f"Would email an unsubscribe request to {mail['address']}"}
            self.graph.send_mail(mail["address"], mail["subject"], mail["body"])
            self.store.mark_unsubscribed(address, "mailto")
            return {"status": "done", "outcome": f"Unsubscribe request emailed to {mail['address']}"}
        if refused:
            notes.append(f"Didn't email {refused}: that's not a recognisable unsubscribe address, and you've "
                         f"never opened anything from this sender. Replying would only confirm your address "
                         f"is real. Block them instead")

        # The one-click URL is a machine endpoint (RFC 8058), not a web page.
        # Plenty of senders answer a browser opening it with a 404, so if the
        # one-click POST failed, hand over any *other* link in preference to it.
        posted = sender["https"][0] if (sender["kind"] == "oneclick" and sender["https"]) else None
        links = [u for u in sender["https"] + sender["http"] if u != posted]
        endpoint_only = False
        if not links and posted:
            links, endpoint_only = [posted], True

        if links:
            if self.dry_run:
                return {"status": "dry_run", "outcome": "Would ask you to open the unsubscribe page"}
            self.store.mark_unsubscribed(address, "link")
            note = (" " + "; ".join(notes)) if notes else ""
            if endpoint_only:
                note += (" This sender only publishes an automated unsubscribe address, so that link may"
                         " not open as a web page — if it doesn't, unsubscribe from the email itself or block them.")
            return {"status": "needs_you", "outcome": "Open the sender's unsubscribe page to finish." + note,
                    "link": links[0]}

        return {"status": "failed", "outcome": "; ".join(notes) or "No working unsubscribe option found"}

    # ------------------------------------------------------------------ moves
    def _move_note(self, sender, folder, label) -> str:
        ids = sender["ids"]
        if not ids:
            return ""
        if self.dry_run:
            return f". Would move {len(ids)} old emails to {label}"
        destination = self.graph.well_known_folder_id(folder) or folder
        origin = dict(zip(ids, sender.get("folders") or []))
        moved, failed = self.graph.move_messages(ids, destination)
        if moved and not self.dry_run:
            self.store.record_moves(self._run_id(), [(new, origin.get(old, "inbox")) for old, new in moved])
        note = f". Moved {len(moved)} old emails to {label}"
        return note + (f" ({failed} couldn't be moved)" if failed else "")

    # ------------------------------------------------------------------ block
    def _block(self, sender) -> dict:
        if self.dry_run:
            return {"status": "dry_run",
                    "outcome": f"Would add to the MailSift blocklist and move {len(sender['ids'])} emails to Junk"}
        # The rule is written once for the whole batch in _finish_blocks().
        return {"status": "pending", "outcome": "Blocking…", "move_note": self._move_note(sender, "junkemail", "Junk")}

    def _finish_blocks(self, results):
        addresses = [r["address"] for r in results]
        try:
            self.blocklist.sync(add=addresses)
        except GraphError as exc:
            for r in results:
                r.update(status="failed", outcome=f"Couldn't update the blocklist rule ({exc.message})" + r.pop("move_note", ""))
                self.store.log(r["address"], r["name"], "block", "failed", r["outcome"], False)
            return
        for r in results:
            r.update(status="done", outcome="Blocked: future emails go to Junk" + r.pop("move_note", ""))
            self.store.mark_blocked(r["address"])
            if not self.dry_run:
                self.store.record_block(self._run_id(), r["address"])
            self.store.log(r["address"], r["name"], "block", "done", r["outcome"], False)

    # ------------------------------------------------------------------- undo
    def undo(self, run_id, progress=None) -> dict:
        """Moves mail from a clean-up back where it was and lifts its blocks."""
        progress = progress or (lambda **_: None)
        moves, blocks = self.store.run_items(run_id)
        by_folder = defaultdict(list)
        for message_id, folder in moves:
            by_folder[folder].append(message_id)

        restored = failed = 0
        steps = len(by_folder) + (1 if blocks else 0)
        done = 0
        for folder, ids in by_folder.items():
            try:
                moved, bad = self.graph.move_messages(ids, folder)
                restored += len(moved)
                failed += bad
                # Put back is put back. If the unblock below fails the run stays
                # undoable, and these ids are stale by then, so forget them now.
                if moved:
                    self.store.drop_moves(run_id, [old for old, _new in moved])
            except GraphError:
                failed += len(ids)
            done += 1
            progress(done=done, total=steps)

        unblocked, block_error = 0, None
        if blocks:
            try:
                self.blocklist.sync(remove=blocks)
                self.store.unmark_blocked(blocks)
                unblocked = len(blocks)
            except GraphError as exc:
                block_error = exc.message
            progress(done=steps, total=steps)

        if block_error is None:
            self.store.mark_undone(run_id)
        self.store.log("-", "MailSift", "undo", "failed" if (failed or block_error) else "done",
                       f"restored {restored}, unblocked {unblocked}, failed {failed}", False)
        return {"restored": restored, "failed": failed, "unblocked": unblocked, "block_error": block_error}
