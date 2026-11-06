"""Asking Outlook for four headers instead of all of them.

MailSift reads exactly four headers, but internetMessageHeaders returns
every one — Received chains, DKIM signatures — which is most of what a scan
downloads. Outlook can return named headers individually instead. It can
also refuse, so the scan checks on the real mailbox first and falls back
rather than quietly finding nothing.
"""
import json

import pytest

from mailsift.demo import DemoGraph, seed_demo_store
from mailsift.graph import GraphClient, GraphError
from mailsift.scanner import Scanner
from mailsift.store import Store


def scan_with(cheap):
    graph, store = DemoGraph(delay=0), Store(":memory:")
    seed_demo_store(store)
    graph.cheap_headers = cheap
    return graph, Scanner(graph, store).scan("all")


def shape(result):
    return {s["address"]: (s["count"], s["read"], s["kind"], tuple(sorted(s["reasons"])),
                           s["score"], s["tier"], s["suggest"], s["ignored_count"])
            for s in result.senders}


# -- the cheap request must find exactly the same things ---------------------------
def test_both_ways_of_asking_give_identical_results():
    _, full = scan_with(False)
    _, light = scan_with(True)
    assert full.checked == light.checked
    assert shape(full) == shape(light), "the short request changed what the scan found"


def test_the_unsubscribe_details_survive_too():
    # These decide what apply actually does, so they matter most.
    _, full = scan_with(False)
    _, light = scan_with(True)
    def routes(r):
        return {p["address"]: (tuple(p["https"]), tuple(p["http"]), tuple(p["mailto"]))
                for p in r.private.values()}
    assert routes(full) == routes(light)


REALISTIC = [   # the headers an actual marketing email arrives with
    {"name": "Received", "value": "from mail-40133.srv.example.net (mail-40133.srv.example.net "
                                  "[198.51.100.40]) by EUR05-DB8-obe.outbound.protection.outlook.com "
                                  "with Microsoft SMTP Server (version=TLS1_2, "
                                  "cipher=TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384) id 15.20.7ient.21; "
                                  "Thu, 6 Nov 2026 07:14:22 +0000"},
    {"name": "Received", "value": "from localhost (localhost [127.0.0.1]) by mail-40133.srv.example.net "
                                  "(Postfix) with ESMTP id 4Xk2Qr5Yz3z for <a@b.com>; "
                                  "Thu, 6 Nov 2026 07:14:21 +0000 (UTC)"},
    {"name": "DKIM-Signature", "value": "v=1; a=rsa-sha256; c=relaxed/relaxed; d=example.net; "
                                        "s=sel2026; t=1793000061; bh=47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=; "
                                        "h=From:To:Subject:Date:List-Unsubscribe; "
                                        "b=" + "A1b2C3d4E5f6G7h8" * 16},
    {"name": "MIME-Version", "value": "1.0"},
    {"name": "Content-Type", "value": "multipart/alternative; boundary=\"--_NmP-2a9f1c0e1b-Part_1\""},
    {"name": "Message-ID", "value": "<01020193f1a2b3c4-5d6e7f80-1a2b-3c4d-5e6f-708192a3b4c5-000000@eu-west-1.amazonses.com>"},
    {"name": "X-Mailer", "value": "ExampleSend 4.11.2"},
    {"name": "X-Campaign-ID", "value": "c-20261106-weekly-7781"},
    {"name": "Feedback-ID", "value": "1.eu-west-1.7m8n9o0p1q2r3s4t5u6v==:AmazonSES"},
    {"name": "Authentication-Results", "value": "spf=pass (sender IP is 198.51.100.40) "
                                                "smtp.mailfrom=example.net; dkim=pass (signature was verified) "
                                                "header.d=example.net; dmarc=pass action=none "
                                                "header.from=example.net; compauth=pass reason=100"},
    {"name": "List-Id", "value": "<weekly.example.net>"},
    {"name": "List-Unsubscribe", "value": "<https://example.net/u/7781>, <mailto:unsub-7781@example.net>"},
    {"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
]


def test_it_really_does_download_less():
    graph = DemoGraph(delay=0)
    graph.messages[0]["internetMessageHeaders"] = REALISTIC
    heavy = next(graph.list_messages({"$filter": None}))
    light = next(graph.list_messages({"$filter": None, "$expand": graph.header_expand()}))
    assert "internetMessageHeaders" not in light
    before, after = len(json.dumps(heavy)), len(json.dumps(light))
    assert after < before / 2, f"{before} -> {after} bytes a message"
    print(f"\n  a realistic message: {before} bytes -> {after} ({100 - 100 * after // before}% less)")


def test_a_message_with_no_such_headers_is_handled_not_refetched():
    # Outlook leaves the field out entirely rather than sending an empty one.
    # Treating that as "unknown" would fetch every message again, one by one.
    graph, store = DemoGraph(delay=0), Store(":memory:")
    graph.get_message_headers = lambda mid: pytest.fail("refetched headers one message at a time")
    personal = [m for m in graph.messages
                if not any(h["name"] == "List-Unsubscribe" for h in m["internetMessageHeaders"])]
    assert personal, "the demo mailbox needs some non-list mail for this to mean anything"
    result = Scanner(graph, store).scan("all")
    assert result.other_senders >= 1


# -- the check, and what happens when it fails ---------------------------------------
def make_client(responses):
    g = GraphClient(lambda: "t", session_factory=lambda: None, sleep=lambda s: None)
    g.request = lambda method, url, **kw: responses.pop(0) if responses else {"value": []}
    return g


def msg(full_headers, props):
    return {"id": "1",
            "internetMessageHeaders": [{"name": n, "value": v} for n, v in full_headers.items()],
            "singleValueExtendedProperties": [
                {"id": f"String {GraphClient._HEADER_NAMESPACE} Name {n}", "value": v}
                for n, v in props.items()]}


def test_the_check_passes_when_both_agree():
    both = {"List-Unsubscribe": "<mailto:x@y.com>", "List-Id": "<news.y.com>"}
    client = make_client([{"value": [msg(both, both), msg({}, {})]}])
    assert client.cheap_headers_work("2026-01-01T00:00:00Z") is True


def test_a_wrong_property_id_is_caught_rather_than_silently_finding_nothing():
    # The dangerous failure: Outlook accepts the request but matches nothing,
    # so every sender would look like ordinary mail and the scan find none.
    client = make_client([{"value": [msg({"List-Unsubscribe": "<mailto:x@y.com>"}, {})]}])
    assert client.cheap_headers_work("2026-01-01T00:00:00Z") is False


def test_a_truncated_or_altered_value_is_caught():
    client = make_client([{"value": [msg({"List-Unsubscribe": "<mailto:full@y.com>"},
                                         {"List-Unsubscribe": "<mailto:tru"})]}])
    assert client.cheap_headers_work("2026-01-01T00:00:00Z") is False


def test_outlook_refusing_the_request_falls_back():
    g = GraphClient(lambda: "t", session_factory=lambda: None, sleep=lambda s: None)

    def refuse(method, url, **kw):
        raise GraphError(400, "Invalid $expand")

    g.request = refuse
    assert g.cheap_headers_work("2026-01-01T00:00:00Z") is False


def test_no_evidence_means_the_slow_certain_route():
    # Nothing recent carries any of the four, so there's nothing to compare.
    client = make_client([{"value": [msg({}, {}), msg({}, {})]}])
    assert client.cheap_headers_work("2026-01-01T00:00:00Z") is False


def test_the_check_is_asked_in_the_same_shape_the_scan_uses():
    # Combining $expand with $filter and $orderby is what Outlook is most
    # likely to refuse, so the check has to use all three or it proves nothing.
    seen = {}
    g = GraphClient(lambda: "t", session_factory=lambda: None, sleep=lambda s: None)

    def capture(method, url, **kw):
        seen.update(kw.get("params") or {})
        return {"value": []}

    g.request = capture
    g.cheap_headers_work("2026-01-01T00:00:00Z")
    assert "$expand" in seen and "$filter" in seen and "$orderby" in seen
    assert "singleValueExtendedProperties" in seen["$expand"]
    for header in GraphClient.WANTED_HEADERS:
        assert header in seen["$expand"]


def test_a_failed_check_still_gives_a_correct_scan():
    graph, store = DemoGraph(delay=0), Store(":memory:")
    seed_demo_store(store)
    graph.cheap_headers = False
    assert shape(Scanner(graph, store).scan("all")) == shape(scan_with(True)[1])
