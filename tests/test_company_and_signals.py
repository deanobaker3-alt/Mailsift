"""Company grouping, Sent Items, the trend data, and what happens when the
optional parts fail. The failure cases matter most: none of the new signals
is allowed to take a scan down."""
import pytest

from mailsift.company import company_of
from mailsift.demo import FADED_SENDER, DemoGraph, seed_demo_store
from mailsift.graph import GraphClient, GraphError
from mailsift.scanner import Scanner
from mailsift.store import Store


def key(address, name="X"):
    return company_of(address, name)["company"]


# -- grouping ------------------------------------------------------------------
def test_one_brand_on_several_addresses_groups():
    assert key("news@brand.com") == key("offers@mail.brand.com") == key("hello@brand.com")


def test_uk_style_suffixes_dont_merge_unrelated_companies():
    assert key("a@crustandco.co.uk") != key("b@bramblehome.co.uk")
    assert key("x@dvla.gov.uk") != key("y@hmrc.gov.uk")


def test_email_platforms_never_merge_different_brands():
    # Angel Clothing really did arrive from soundest.email, Omnisend's domain.
    angel = key("communications@6ae0a43d16c3.soundest.email", "Angel Clothing")
    other = key("communications@99ff00aa11bb.soundest.email", "Someone Else Ltd")
    assert angel != other
    # ...but one brand's two lists on the same platform still group.
    assert angel == key("offers@1234abcd.soundest.email", "Angel Clothing")


def test_platform_sender_without_a_name_stands_alone():
    assert key("a@mailchimpapp.net", "") != key("b@mailchimpapp.net", "")


@pytest.mark.parametrize("domain", ["gmail.com", "hotmail.co.uk", "outlook.com", "icloud.com"])
def test_personal_addresses_are_never_grouped(domain):
    assert key(f"alice@{domain}", "Newsletter") != key(f"bob@{domain}", "Newsletter")


@pytest.mark.parametrize("address", ["", "no-at-sign", "@", "x@", "  SPACES@Brand.COM  "])
def test_odd_addresses_dont_crash(address):
    result = company_of(address, "Name")
    assert result["company"] and result["company_label"]


# -- Sent Items through Graph ----------------------------------------------------
class Resp:
    def __init__(self, payload):
        self.status_code, self.headers, self.content, self.text = 200, {}, b"x", ""
        self._payload = payload

    def json(self):
        return self._payload


class Session:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), 0

    def request(self, method, url, **kw):
        self.calls += 1
        return Resp(self.pages.pop(0))


def test_sent_recipients_reads_to_cc_bcc_and_ignores_rubbish():
    page = {"value": [
        {"toRecipients": [{"emailAddress": {"address": "Studio@KilnAndClay.co.uk"}}],
         "ccRecipients": [{"emailAddress": {"address": "friend@example.com"}}, None, {}],
         "bccRecipients": [{"emailAddress": {"address": "not-an-address"}}]},
        {"toRecipients": None},
        {},
    ]}
    session = Session([page])
    g = GraphClient(lambda: "t", session_factory=lambda: session, sleep=lambda s: None)
    assert g.sent_recipients() == {"studio@kilnandclay.co.uk", "friend@example.com"}


def test_sent_recipients_stops_at_the_limit_without_fetching_more_pages():
    many = {"value": [{"toRecipients": [{"emailAddress": {"address": f"p{i}@x.com"}}]} for i in range(200)],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages?$skiptoken=2"}
    session = Session([many, many, many])
    g = GraphClient(lambda: "t", session_factory=lambda: session, sleep=lambda s: None)
    assert len(g.sent_recipients(limit=150)) == 150
    assert session.calls == 1


# -- the scan, end to end ----------------------------------------------------------
@pytest.fixture
def mailbox():
    store = Store(":memory:")
    seed_demo_store(store)
    return DemoGraph(delay=0), store


def by_address(result):
    return {s["address"]: s for s in result.senders}


def test_scan_marks_who_you_have_emailed_and_leaves_them_alone(mailbox):
    graph, store = mailbox
    result = Scanner(graph, store).scan("12m")
    s = by_address(result)
    assert result.sent_checked is True
    assert s["studio@kilnandclay.co.uk"]["emailed"] is True
    assert s["studio@kilnandclay.co.uk"]["tier"] == "leave"
    assert s["offers@crustandco.co.uk"]["emailed"] is False


def test_scan_detects_the_faded_sender(mailbox):
    graph, store = mailbox
    s = by_address(Scanner(graph, store).scan("12m"))[FADED_SENDER]
    assert s["trend"] == "faded"
    assert s["recent_opened"] == 0 and s["older_opened"] > 0
    assert s["recent_count"] + s["older_count"] == s["count"]


def test_scan_groups_the_demo_brand(mailbox):
    graph, store = mailbox
    s = by_address(Scanner(graph, store).scan("12m"))
    assert s["offers@crustandco.co.uk"]["company"] == s["rewards@crustandco.co.uk"]["company"]
    assert s["offers@crustandco.co.uk"]["company"] != s["news@fitfuel.co.uk"]["company"]


def test_sent_items_failing_never_breaks_the_scan(mailbox):
    graph, store = mailbox

    def broken(limit=2000):
        raise GraphError(503, "Service unavailable")

    graph.sent_recipients = broken
    logs = []
    result = Scanner(graph, store).scan("12m", log=logs.append)
    assert result.senders, "the scan itself must still complete"
    assert result.sent_checked is False
    assert all(s["emailed"] is None for s in result.senders)
    assert all("score" in s and "tier" in s for s in result.senders)
    assert any("couldn't read" in line for line in logs)


def test_unexpected_crash_in_sent_items_is_contained_too(mailbox):
    graph, store = mailbox
    graph.sent_recipients = lambda limit=2000: (_ for _ in ()).throw(RuntimeError("boom"))
    assert Scanner(graph, store).scan("12m").sent_checked is False


def test_graph_without_sent_items_support_still_scans(mailbox):
    graph, store = mailbox
    graph.sent_recipients = None
    result = Scanner(graph, store).scan("12m")
    assert result.senders and result.sent_checked is False


def test_cancelled_scan_doesnt_bother_with_sent_items(mailbox):
    graph, store = mailbox
    calls = []
    graph.sent_recipients = lambda limit=2000: calls.append(1) or set()
    Scanner(graph, store).scan("12m", cancelled=lambda: True)
    assert calls == []


def test_empty_mailbox(mailbox):
    _, store = mailbox
    empty = DemoGraph(delay=0)
    empty.messages = []
    result = Scanner(empty, store).scan("all")
    assert result.senders == [] and result.checked == 0
