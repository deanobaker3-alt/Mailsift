"""MailSift's blocked senders, kept as a few Outlook inbox rules.

Outlook limits how many inbox rules a mailbox can have, so instead of one
rule per blocked sender MailSift keeps a single "MailSift blocklist" rule
(split into numbered parts if it ever gets very long). Each matches senders
exactly and moves their mail to Junk.

The rules themselves are the source of truth: the blocklist survives even if
MailSift's local history is wiped, and rules left by older versions
("MailSift block: <address>", one per sender) are folded in and removed.
"""
from __future__ import annotations

import re

from .graph import GraphError
from .headers import normalise_address

BLOCKLIST_NAME = "MailSift blocklist"
LEGACY_PREFIX = "MailSift block: "
# Matched exactly, not by prefix: a rule of the user's own called something
# like "MailSift blocklist backup" is theirs, and must not be rewritten or
# deleted as if MailSift had created it.
OURS = re.compile(r"^MailSift blocklist(?: \(\d{1,4}\))?$")
CHUNK = 100  # addresses per rule: keeps each rule well inside Outlook's size limits
MAX_ADDRESS = 254  # RFC 5321; Outlook rejects anything longer


def usable(address: str) -> str:
    """The address as Outlook will accept it in a rule, or "" if it won't.

    A sender can put anything in a From header. One arrived wrapped in angle
    brackets, Outlook refused the rule containing it, and every block in that
    clean-up failed with it. Nothing unusable gets as far as a rule now.
    """
    clean = normalise_address(address)
    if not clean or len(clean) > MAX_ADDRESS or not clean.isascii():
        return ""
    return clean


def _name(index: int) -> str:
    return BLOCKLIST_NAME if index == 0 else f"{BLOCKLIST_NAME} ({index + 1})"


def _addresses(rule) -> set[str]:
    conditions = rule.get("conditions") or {}
    found = set()
    for entry in conditions.get("fromAddresses") or []:
        address = ((entry or {}).get("emailAddress") or {}).get("address")
        if address:
            found.add(address.strip().lower())
    for text in conditions.get("senderContains") or []:  # very old v1.0 rules
        if "@" in (text or ""):
            found.add(text.strip().lower())
    return found


class Blocklist:
    def __init__(self, graph):
        self.graph = graph

    def _rules(self):
        ours, legacy = [], []
        for rule in self.graph.list_rules():
            name = rule.get("displayName") or ""
            if OURS.match(name):
                ours.append(rule)
            elif name.startswith(LEGACY_PREFIX):
                legacy.append(rule)
        return ours, legacy

    def current(self) -> set[str]:
        ours, legacy = self._rules()
        return set().union(*(_addresses(r) for r in ours + legacy)) if ours or legacy else set()

    def sync(self, add=(), remove=()) -> tuple[set[str], dict[str, str]]:
        """Adds/removes addresses and rewrites the rules.

        Returns (blocklist, rejected): the addresses now blocked, and any that
        couldn't be, each with the reason. Writing is per-rule, so a value
        Outlook dislikes costs only the senders in that one rule rather than
        every block in the clean-up.

        New rules are written before old ones are deleted, so there's never a
        moment where a blocked sender gets through.
        """
        rejected: dict[str, str] = {}
        wanted = set()
        for address in add:
            clean = usable(address)
            if clean:
                wanted.add(clean)
            else:
                rejected[address] = "Outlook won't accept that address in a rule"

        ours, legacy = self._rules()
        existing = set().union(*(_addresses(r) for r in ours + legacy)) if ours or legacy else set()
        # An unusable address already sitting in a rule (written by an older
        # version) would break every later write, so it is dropped silently.
        existing = {a for a in (usable(a) for a in existing) if a}
        desired = (existing | wanted) - {a for a in (usable(a) for a in remove) if a}
        ordered = sorted(desired)
        chunks = [ordered[i:i + CHUNK] for i in range(0, len(ordered), CHUNK)]

        junk = self.graph.well_known_folder_id("junkemail") or "junkemail"
        by_name = {r.get("displayName"): r for r in ours}
        all_rules = ours + legacy
        next_sequence = max((r.get("sequence") or 0 for r in all_rules), default=0) + 1
        keep = set()
        for index, chunk in enumerate(chunks):
            name = _name(index)
            body = {
                "displayName": name,
                "isEnabled": True,
                "conditions": {"fromAddresses": [{"emailAddress": {"address": a}} for a in chunk]},
                "actions": {"moveToFolder": junk, "stopProcessingRules": True},
            }
            try:
                if name in by_name:
                    if _addresses(by_name[name]) != set(chunk):
                        self.graph.update_rule(by_name[name]["id"], body)
                else:
                    self.graph.create_rule({**body, "sequence": next_sequence})
                    next_sequence += 1
            except GraphError as exc:
                # Only this rule's senders are affected; the others still go in.
                for address in chunk:
                    if address in wanted:
                        rejected[address] = f"Outlook refused the rule ({exc.message})"
                desired -= set(chunk) - existing
            keep.add(name)

        for rule in ours:
            if rule.get("displayName") not in keep:
                self.graph.delete_rule(rule["id"])
        for rule in legacy:
            self.graph.delete_rule(rule["id"])
        return desired, rejected
