"""Which senders belong to the same company, so they can be dealt with as one.

Grouping is by registrable domain (news@brand.com, offers@mail.brand.com and
brand.com all group together), with two exceptions where a shared domain says
nothing about who's sending:

* Email platforms. Angel Clothing arrives from a soundest.email address,
  which is Omnisend's: so does every other Omnisend customer. Senders on
  these domains are grouped by their display name instead, so two of one
  brand's lists still group but two different brands never do.
* Free-mail and personal domains. Two people on hotmail.co.uk are not a
  company. These are never grouped at all.

Getting this wrong in the merging direction is the costly one (one click
could act on unrelated senders), so when in doubt it doesn't group.
"""
from __future__ import annotations

import re

from .classify import registrable_domain

# Shared sending domains of email platforms. Registrable domains only.
PLATFORMS = {
    "mailchimpapp.net", "mcsv.net", "mcdlv.net", "rsgsv.net", "list-manage.com", "mailchimp.com",
    "sendgrid.net", "sendgrid.com", "amazonses.com", "mailgun.org", "mailgun.net",
    "sparkpostmail.com", "mandrillapp.com", "klaviyomail.com", "klaviyo.com", "soundest.email",
    "omnisend.com", "sendinblue.com", "brevo.com", "brevosend.com", "constantcontact.com",
    "ccsend.com", "ctctcdn.com", "createsend.com", "cmail19.com", "cmail20.com", "exacttarget.com",
    "hubspotemail.net", "hubspot.com", "hs-email.com", "substack.com", "beehiiv.com", "ghost.io",
    "convertkit.com", "ck.page", "kit.com", "mailerlite.com", "mlsend.com", "iterable.com",
    "customeriomail.com", "customer.io", "postmarkapp.com", "mailjet.com", "getresponse.com",
    "aweber.com", "activecampaign.com", "acems1.com", "emailoctopus.com", "squarespace-mail.com",
    "shopifyemail.com", "shopify.com", "wix.com", "wixemails.com", "eventbrite.com",
    "googlegroups.com", "groups.io", "freelists.org", "medium.com", "patreon.com", "buttondown.email",
}

# Domains where an address is a person or a household, never a company.
PERSONAL = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com",
    "live.co.uk", "msn.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "icloud.com", "me.com",
    "mac.com", "aol.com", "aol.co.uk", "proton.me", "protonmail.com", "pm.me", "gmx.com", "gmx.co.uk",
    "mail.com", "zoho.com", "btinternet.com", "sky.com", "virginmedia.com", "talktalk.net",
    "ntlworld.com", "blueyonder.co.uk", "fastmail.com", "tutanota.com", "hey.com",
}


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def company_of(address: str, display_name: str) -> dict:
    """{"company": grouping key, "company_label": what to call the group}.

    Senders sharing a key belong together. A key unique to one address means
    "on its own"."""
    address = (address or "").strip().lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    reg = registrable_domain(domain) if domain else ""
    if not reg or reg in PERSONAL:
        return {"company": f"addr:{address}", "company_label": address or "unknown sender"}
    if reg in PLATFORMS:
        name = _squash(display_name)
        if not name:   # no name to go on: don't risk merging strangers
            return {"company": f"addr:{address}", "company_label": address}
        return {"company": f"name:{name}@{reg}", "company_label": f"{display_name} (via {reg})"}
    return {"company": f"dom:{reg}", "company_label": reg}
