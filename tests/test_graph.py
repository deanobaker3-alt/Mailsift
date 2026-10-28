import pytest

from mailsift.graph import GraphClient, GraphError


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.content = b"x"
        self.text = ""

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs["headers"].get("Authorization")))
        return self.responses.pop(0)


def client(responses):
    session = FakeSession(responses)
    return GraphClient(lambda: "TOKEN", session_factory=lambda: session, sleep=lambda s: None), session


def test_follows_graph_nextlink():
    g, s = client([
        FakeResponse(payload={"value": [1], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=x"}),
        FakeResponse(payload={"value": [2]}),
    ])
    assert list(g.get_all("/me/messages")) == [1, 2]
    assert s.calls[1][1].startswith("https://graph.microsoft.com/v1.0/")


def test_never_sends_token_to_foreign_nextlink():
    g, s = client([FakeResponse(payload={"value": [1], "@odata.nextLink": "https://evil.example/steal"})])
    with pytest.raises(GraphError):
        list(g.get_all("/me/messages"))
    assert all(url.startswith("https://graph.microsoft.com/") for _, url, _ in s.calls)


def test_honours_retry_after():
    waits = []
    session = FakeSession([FakeResponse(429, headers={"Retry-After": "7"}), FakeResponse(payload={"ok": 1})])
    g = GraphClient(lambda: "T", session_factory=lambda: session, sleep=waits.append)
    assert g.request("GET", "/me") == {"ok": 1}
    assert waits == [7]


@pytest.mark.parametrize("payload", [
    ["unexpected", "list"],                       # not an object at all
    {"error": "a bare string"},                   # error isn't an object
    {"error": {"code": "x"}},                     # object with no message
    {"error": {"message": None}},                 # message isn't text
    "just a string",
])
def test_odd_shaped_error_bodies_still_raise_graph_error(payload):
    # A malformed error reply must not escape as AttributeError: everything
    # upstream catches GraphError and would otherwise show "Something went wrong".
    g, _ = client([FakeResponse(status=400, payload=payload)])
    with pytest.raises(GraphError) as caught:
        g.request("GET", "/me")
    assert caught.value.status == 400


def test_error_message_is_used_when_graph_sends_one():
    g, _ = client([FakeResponse(status=404, payload={"error": {"message": "Folder not found"}})])
    with pytest.raises(GraphError) as caught:
        g.request("GET", "/me")
    assert caught.value.message == "Folder not found"
