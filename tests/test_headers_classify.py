from mailsift.classify import classify, lookalike_brand, registrable_domain
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
        "address": "leave@example.com", "subject": "Remove me", "body": "unsubscribe"}
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
