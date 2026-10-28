from mailsift.classify import (ALLOWED_ACTIONS, classify, lookalike_brand, random_looking_suffix,
                               registrable_domain)
from mailsift.headers import (has_one_click, header_map, parse_auth_results, parse_list_unsubscribe,
                              parse_mailto, split_unsubscribe_uris)

PASS = {"spf": "pass", "dkim": "pass", "dmarc": "pass", "compauth": "pass"}


def test_list_unsubscribe_folded_and_split():
    value = "<https://example.com/u?id=1\r\n 23>,\r\n <mailto:leave@example.com?subject=unsubscribe>, <http://old.example.com/u>"
    uris = parse_list_unsubscribe(value)
    assert uris[0] == "https://example.com/u?id=123"
    https, http, mailto = split_unsubscribe_uris(uris)
    assert https == ["https://example.com/u?id=123"]
    assert http == ["http://old.example.com/u"]
    assert mailto == ["mailto:leave@example.com?subject=unsubscribe"]


def test_one_click():
    assert has_one_click("List-Unsubscribe=One-Click")
    assert has_one_click("list-unsubscribe = one-click")
    assert not has_one_click(None)
    assert not has_one_click("something else")


def test_auth_results_uses_topmost_header_only():
    hmap = header_map([
        {"name": "Authentication-Results", "value": "spf=fail smtp.mailfrom=x.com; dkim=none; dmarc=fail action=none header.from=x.com;compauth=fail reason=001"},
        {"name": "Authentication-Results", "value": "spf=pass; dkim=pass; dmarc=pass"},  # forged lower down
    ])
    verdict = parse_auth_results(hmap["authentication-results"])
    assert verdict == {"spf": "fail", "dkim": "none", "dmarc": "fail", "compauth": "fail"}


def test_parse_mailto():
    assert parse_mailto("mailto:leave@example.com?subject=Remove%20me") == {
        "address": "leave@example.com", "subject": "unsubscribe", "body": "unsubscribe"}
    assert parse_mailto("mailto:a@x.com,b@y.com") is None
    assert parse_mailto("https://example.com") is None


def test_registrable_domain():
    assert registrable_domain("mail.news.crustandco.co.uk") == "crustandco.co.uk"
    assert registrable_domain("email.amazon.com") == "amazon.com"


def test_lookalikes():
    assert lookalike_brand("amaz0n-prize.top") == ("amazon", "homoglyph")
    assert lookalike_brand("paypa1.com") == ("paypal", "homoglyph")
    assert lookalike_brand("amazon-rewards.com") == ("amazon", "name")
    assert lookalike_brand("amazon.co.uk") is None
    assert lookalike_brand("crustandco.co.uk") is None


def _cls(**kw):
    base = dict(address="offers@crustandco.co.uk", display_name="Crust & Co", auth=PASS, one_click=True,
                https=["https://u.crustandco.co.uk/x"], http=[], mailto=[], junk_count=0, count=10)
    base.update(kw)
    return classify(**base)


def test_legit_one_click():
    assert _cls()["kind"] == "oneclick"


def test_mailto_and_link_and_none():
    assert _cls(one_click=False, https=[], mailto=["mailto:x@crustandco.co.uk"])["kind"] == "mailto"
    assert _cls(one_click=False)["kind"] == "link"
    assert _cls(one_click=False, https=[])["kind"] == "none"


def test_dmarc_fail_is_dodgy():
    result = _cls(auth={"dmarc": "fail", "spf": "fail"})
    assert result["kind"] == "dodgy"
    assert any("DMARC" in r for r in result["reasons"])


def test_lookalike_is_dodgy_even_if_authenticated():
    # attackers can pass SPF/DKIM for their own lookalike domain
    result = _cls(address="claim@amaz0n-prize.com", display_name="Rewards")
    assert result["kind"] == "dodgy"


def test_display_name_impersonation():
    result = _cls(address="noreply@randomshop.com", display_name="PayPal Security")
    assert result["kind"] == "dodgy"
    assert any("PayPal" in r for r in result["reasons"])


def test_everyday_words_not_impersonation():
    assert _cls(address="news@weeklyoutlook.co.uk", display_name="The Weekly Outlook")["kind"] == "oneclick"


def test_suspicious_tld():
    assert _cls(address="hi@deals.xyz")["kind"] == "dodgy"


def test_no_auth_is_unverified():
    assert _cls(auth={})["kind"] == "unverified"


def test_ip_link_is_dodgy():
    assert _cls(https=["https://203.0.113.9/u"])["kind"] == "dodgy"


def test_mostly_junk_and_unauthenticated():
    assert _cls(auth={"spf": "softfail", "dmarc": "none"}, junk_count=8, count=10)["kind"] == "dodgy"


def test_mailto_ignores_sender_supplied_text():
    # a hostile header must not choose what gets sent from your account
    m = parse_mailto("mailto:leave@example.com?subject=Invoice%20overdue&body=Pay%20at%20evil.example")
    assert m == {"address": "leave@example.com", "subject": "unsubscribe", "body": "unsubscribe"}


# --- Impersonation that passes authentication -------------------------------
# Real case from a live mailbox: a phishing email calling itself "TV LICENSING"
# with an unsubscribe at an unrelated domain. It authenticated cleanly, because
# the spammer owned the sending domain, so authentication alone cleared it for
# unsubscribing. Authentication proves who sent it, not that they're honest.

def test_random_looking_suffix_spots_filter_evasion():
    assert random_looking_suffix("TV LICENSINGoz6Fc") == "oz6Fc"
    assert random_looking_suffix("NEWSLETTERa1Bc2") == "a1Bc2"


def test_random_looking_suffix_leaves_ordinary_names_alone():
    for name in ["Crust & Co", "Formula1", "Channel 4", "iPhone 15 Deals", "iPhone15",
                 "Log4j Weekly", "Web3 Daily", "ROYAL MAIL", "Marks and Spencer",
                 "Swindon Wildcats", "3M Updates", "O2 Priority", "", "Issue 42"]:
        assert random_looking_suffix(name) == "", name


def test_authenticated_impersonator_cannot_be_unsubscribed_from():
    result = _cls(address="noreply@enhanceportland.com", display_name="TV LICENSINGoz6Fc",
                  auth=PASS, one_click=False, https=[],
                  mailto=["mailto:babbette_angelico@enhanceportland.com"])
    assert result["kind"] == "dodgy"
    assert "unsub" not in ALLOWED_ACTIONS[result["kind"]]
    assert "unsubdel" not in ALLOWED_ACTIONS[result["kind"]]
    assert ALLOWED_ACTIONS[result["kind"]] == {"keep", "block"}


def test_borrowed_brand_name_is_dodgy_even_without_a_random_suffix():
    result = _cls(address="noreply@enhanceportland.com", display_name="TV Licensing", auth=PASS)
    assert result["kind"] == "dodgy"
    assert any("TV Licensing".lower() in r.lower() for r in result["reasons"])


def test_brand_sending_from_its_own_domain_is_fine():
    result = _cls(address="noreply@tvlicensing.co.uk", display_name="TV Licensing", auth=PASS)
    assert result["kind"] == "oneclick"
    assert result["reasons"] == []


def test_brands_that_split_their_name_across_the_domain_are_not_impersonators():
    # Real senders whose domain only matches once the dots are removed.
    for name, addr in [("Octopus Energy", "hello@octopus.energy"),
                       ("Plusnet", "news@plus.net"),
                       ("Just Eat", "orders@just-eat.co.uk")]:
        result = _cls(address=addr, display_name=name, auth=PASS)
        assert result["kind"] == "oneclick", (name, result["reasons"])
