"""A sender writes these headers, so a sender can choose the worst possible
value. Two of the parsers used regexes that rescanned from every position
when a match failed, which is quadratic: a 100,000-character From header
took 48 seconds to reject, and 20,000 unclosed "<" in List-Unsubscribe took
1.5 seconds. One crafted email could have stalled a whole scan.

These tests pin the cost down. They assert a time budget rather than an
exact shape, because the point is that no sender can make the parser work
hard, however they write the header.
"""
import time

import pytest

from mailsift.classify import lookalike_brand, registrable_domain
from mailsift.headers import (MAX_HEADER, emails_in, has_one_click, normalise_address,
                              parse_auth_results, parse_list_unsubscribe, parse_mailto)

# Seconds. Deliberately loose. This started at 0.25 and flaked the moment it
# ran next to a busy pip install, and a shared CI runner is busier than that.
# The bug it guards took 48 seconds, so 2 s still catches it twenty-four
# times over while leaving enough headroom that a slow machine can't turn a
# green build red. The shape of the cost is checked separately, below, which
# is the part that actually detects a return of the bug.
BUDGET = 2.0


def hostile():
    return {
        "100k non-address":      "A" * 100_000,
        "no at-sign":            "a" * 50_000 + "!",
        "long url in brackets":  "<https://" + "a" * 60_000 + ".com/u>",
        "unclosed brackets":     "<" * 50_000,
        "closed then not":       "<>" * 25_000,
        "nested brackets":       "<" * 25_000 + ">" * 25_000,
        "huge mailto":           "<mailto:" + "b" * 60_000 + "@x.com>",
        "many entries":          "<mailto:a@b.com>, <https://x.com/u>, " * 5_000,
        "whitespace":            "\t \n" * 30_000,
        "semicolons":            ";" * 50_000,
        "at-signs":              "a@" * 30_000,
        "dots":                  "." * 50_000,
    }


PARSERS = [parse_list_unsubscribe, normalise_address, parse_mailto,
           has_one_click, emails_in, registrable_domain, lookalike_brand,
           lambda v: parse_auth_results([v])]


@pytest.mark.parametrize("name,value", list(hostile().items()))
def test_no_header_can_make_a_parser_work_hard(name, value):
    for parser in PARSERS:
        started = time.monotonic()
        parser(value)                      # must not raise either
        spent = time.monotonic() - started
        assert spent < BUDGET, (
            f"{getattr(parser, '__name__', parser)} took {spent:.2f}s on {name!r} "
            f"({len(value)} chars): a sender chooses this header")


def test_the_cost_does_not_square_as_the_header_grows():
    """The tell for the bug that was here: four times the work for twice
    the input. Linear is roughly two."""
    def cost(n):
        value = "<" * n
        started = time.monotonic()
        for _ in range(3):
            parse_list_unsubscribe(value)
            normalise_address(value)
        return (time.monotonic() - started) / 3

    small, large = cost(4_000), cost(16_000)
    # 4x the input. Quadratic would be ~16x; allow a loose ceiling so this
    # measures shape, not the speed of the machine it runs on.
    assert large < max(small * 8, 0.02), f"{small:.4f}s -> {large:.4f}s for 4x the input"


def test_an_absurdly_long_header_is_refused_not_truncated():
    """Truncating would be worse than refusing: half a domain is a
    *different* address, and these end up in inbox rules."""
    real = "news@example.com"
    assert normalise_address(f"Someone <{real}>") == real
    padded = "x" * (MAX_HEADER + 1) + f" <{real}>"
    assert normalise_address(padded) == "", "a header this long should be refused outright"
    assert parse_list_unsubscribe("<https://x.com/u>" + "<" * MAX_HEADER) == []


def test_normal_headers_still_parse_after_the_bounds():
    assert normalise_address("Crust & Co <offers@crustandco.co.uk>") == "offers@crustandco.co.uk"
    assert parse_list_unsubscribe("<https://unsub.x.co.uk/u?id=9>, <mailto:unsub@x.co.uk>") == \
        ["https://unsub.x.co.uk/u?id=9", "mailto:unsub@x.co.uk"]
    assert parse_mailto("mailto:unsub@x.co.uk")["address"] == "unsub@x.co.uk"
    assert has_one_click("List-Unsubscribe=One-Click") is True


def test_bulk_senders_that_ignore_the_rfc_still_parse():
    """The bound on the local part was RFC 5321's 64 characters at first,
    which rejected a real Mailchimp unsubscribe address using 66. Bulk
    senders encode a subscriber id there and do not read the spec, so the
    bound exists to keep the match linear, not to enforce the standard."""
    long_real = ("unsubscribe-mc.us4_6bceaaa7f5856768f4be23c55.2048a49016-997beaab74"
                 "@unsubscribe.mailchimpapp.net")
    assert normalise_address(f"<{long_real}>") == long_real
    assert parse_mailto(f"mailto:{long_real}")["address"] == long_real
