import time

import pytest

from mailsift.app import bundled_client_id, create_app

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
SECRET = "s" * 40


def make(tmp_path, **kw):
    kw.setdefault("demo", True)
    calls = []
    app = create_app(tmp_path, port=PORT, launcher_secret=SECRET, shutdown=lambda: calls.append("bye"), **kw)
    client = app.test_client()
    client.app_ref, client.shutdowns = app, calls
    client.token = app.config["MAILSIFT_TOKEN"]
    return client


def unlock(client):
    key = client.app_ref.config["MAILSIFT_NEW_LAUNCH_KEY"]()
    response = client.get(f"{BASE}/?k={key}")
    assert response.status_code == 303 and response.headers["Location"] == "/"
    return client


@pytest.fixture
def client(tmp_path):
    return unlock(make(tmp_path))


def post(client, path, body=None, token=True):
    headers = {"X-MailSift-Token": client.token} if token else {}
    return client.post(BASE + path, json=body or {}, headers=headers)


def wait_for(client, path, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(BASE + path).get_json()
        if job["state"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.1)
    raise AssertionError("timed out")


# -- the lock -------------------------------------------------------------
def test_locked_without_launch_key(tmp_path):
    c = make(tmp_path)
    page = c.get(BASE + "/").get_data(as_text=True)
    assert c.token not in page and "MailSift" in page          # locked page, no CSRF token
    assert c.get(BASE + "/api/status").status_code == 401
    assert c.get(BASE + "/api/scan").status_code == 401         # sender list is not readable


def test_launch_key_is_single_use(tmp_path):
    c = make(tmp_path)
    key = c.app_ref.config["MAILSIFT_NEW_LAUNCH_KEY"]()
    assert c.get(f"{BASE}/?k={key}").status_code == 303
    other = c.app_ref.test_client()
    assert other.get(f"{BASE}/?k={key}").status_code == 403
    assert other.get(BASE + "/api/status").status_code == 401


def test_bogus_key_rejected(tmp_path):
    c = make(tmp_path)
    assert c.get(f"{BASE}/?k=guess").status_code == 403


def test_cookie_flags(tmp_path):
    c = make(tmp_path)
    key = c.app_ref.config["MAILSIFT_NEW_LAUNCH_KEY"]()
    cookie = c.get(f"{BASE}/?k={key}").headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_unlocked_index_has_token_and_headers(client):
    response = client.get(BASE + "/")
    assert client.token in response.get_data(as_text=True)
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"


def test_foreign_host_rejected(client):
    assert client.get("http://evil.example:8765/api/status").status_code == 403


def test_post_needs_csrf_token_even_with_cookie(client):
    assert post(client, "/api/scan", {"scope": "12m"}, token=False).status_code == 403


def test_launcher_handshake(tmp_path):
    c = make(tmp_path)
    assert c.post(BASE + "/api/launch-key").status_code == 403
    assert c.post(BASE + "/api/launch-key", headers={"X-Launcher-Secret": "wrong" * 8}).status_code == 403
    key = c.post(BASE + "/api/launch-key", headers={"X-Launcher-Secret": SECRET}).get_json()["key"]
    assert c.get(f"{BASE}/?k={key}").status_code == 303


# -- the flow ---------------------------------------------------------------
def test_full_flow(client):
    status = client.get(BASE + "/api/status").get_json()
    assert status["signed_in"] and status["demo"] and status["token_store"] == "encrypted"

    assert post(client, "/api/scan", {"scope": "12m"}).status_code == 200
    job = wait_for(client, "/api/scan")
    assert job["state"] == "done"
    assert any("worker" in line for line in job["log"])
    senders = {s["address"]: s for s in job["senders"]}
    crust = senders["offers@crustandco.co.uk"]
    assert crust["auth"] == {"spf": "pass", "dkim": "pass", "dmarc": "pass"}
    assert crust["per_month"] > 0

    assert post(client, "/api/apply", {"choices": {crust["id"]: "unsub"}}).status_code == 200
    job = wait_for(client, "/api/apply")
    assert job["state"] == "done" and job["results"][0]["status"] == "done"
    assert client.get(BASE + "/api/history").get_json()["items"][0]["address"] == "offers@crustandco.co.uk"


def test_cancel_scan(tmp_path):
    c = unlock(make(tmp_path))
    c.app_ref.config["MAILSIFT_STATE"].graph.delay = 0.3
    post(c, "/api/scan", {"scope": "all"})
    time.sleep(0.2)
    post(c, "/api/scan/cancel")
    assert wait_for(c, "/api/scan")["state"] == "cancelled"


def test_quit_calls_shutdown(client):
    assert post(client, "/api/quit").status_code == 200
    time.sleep(0.6)
    assert client.shutdowns == ["bye"]


def test_forget_wipes_history(client):
    post(client, "/api/scan", {"scope": "12m"})
    senders = wait_for(client, "/api/scan")["senders"]
    post(client, "/api/apply", {"choices": {senders[0]["id"]: "block"}})
    wait_for(client, "/api/apply")
    assert client.get(BASE + "/api/history").get_json()["items"]
    assert post(client, "/api/forget").status_code == 200
    assert client.get(BASE + "/api/history").get_json()["items"] == []
    assert client.get(BASE + "/api/status").get_json()["signed_in"] is False


def test_bad_inputs(client):
    assert post(client, "/api/scan", {"scope": "forever"}).status_code == 400
    assert post(client, "/api/apply", {"choices": {}}).status_code == 400


# -- configuration -------------------------------------------------------------
def test_bundled_client_id(tmp_path):
    f = tmp_path / "client_id.txt"
    f.write_text("# comment\n\n")
    assert bundled_client_id(f) is None
    f.write_text("# comment\n11111111-2222-3333-4444-555555555555\n")
    assert bundled_client_id(f) == "11111111-2222-3333-4444-555555555555"
    f.write_text("not-a-guid\n")
    assert bundled_client_id(f) is None


def test_bundled_id_skips_setup(tmp_path, monkeypatch):
    monkeypatch.setattr("mailsift.app.bundled_client_id", lambda: "11111111-2222-3333-4444-555555555555")
    c = unlock(make(tmp_path, demo=False))
    assert c.get(BASE + "/api/status").get_json()["configured"] is True


def test_config_validates_client_id(tmp_path, monkeypatch):
    monkeypatch.setattr("mailsift.app.bundled_client_id", lambda: None)  # as if no ID were built in
    c = unlock(make(tmp_path, demo=False))
    assert c.get(BASE + "/api/status").get_json()["configured"] is False
    assert post(c, "/api/config", {"client_id": "not-a-guid"}).status_code == 400
    assert post(c, "/api/config", {"client_id": "11111111-2222-3333-4444-555555555555"}).status_code == 200
    status = c.get(BASE + "/api/status").get_json()
    assert status["configured"] is True and status["signed_in"] is False


def test_protect_and_undo_endpoints(client):
    post(client, "/api/scan", {"scope": "12m"})
    senders = {s["address"]: s for s in wait_for(client, "/api/scan")["senders"]}
    crust, amz = senders["offers@crustandco.co.uk"], senders["claim@amaz0n-prize.top"]

    assert post(client, "/api/protect", {"id": crust["id"], "on": True}).get_json()["protected"] is True
    assert client.get(BASE + "/api/scan").get_json()["senders"][0]["protected"] is True

    post(client, "/api/apply", {"choices": {crust["id"]: "unsubdel", amz["id"]: "block"}})
    results = {r["address"]: r for r in wait_for(client, "/api/apply")["results"]}
    assert results[crust["address"]]["status"] == "failed"      # protected
    assert results[amz["address"]]["status"] == "done"

    undo = client.get(BASE + "/api/status").get_json()["undo"]
    assert undo["blocked"] == 1 and undo["moved"] == 12
    assert post(client, "/api/undo", {"run_id": undo["run_id"] + 99}).status_code == 409   # stale id refused
    assert post(client, "/api/undo", {"run_id": undo["run_id"]}).status_code == 200
    job = wait_for(client, "/api/undo")
    assert job["restored"] == 12 and job["unblocked"] == 1
    status = client.get(BASE + "/api/status").get_json()
    assert status["undo"] is None and status["has_results"] is False


def test_fonts_are_served_locally(client):
    for name in ("press-start-2p.woff2", "vt323.woff2", "OFL.txt"):
        assert client.get(f"{BASE}/static/fonts/{name}").status_code == 200
    css = client.get(BASE + "/static/app.css").get_data(as_text=True)
    assert "googleapis" not in css and "/static/fonts/" in css   # no third-party requests


@pytest.mark.parametrize("url,ok", [
    ("https://ko-fi.com/deaner", True),
    ("https://www.buymeacoffee.com/deaner", True),
    ("https://github.com/sponsors/deanobaker3-alt", True),
    ("https://paypal.me/deaner", True),
    ("http://ko-fi.com/deaner", False),                      # not https
    ("https://ko-fi.com.evil.example/deaner", False),        # lookalike host
    ("https://evil.example/ko-fi.com/deaner", False),
    ("javascript:alert(1)", False),
    ("https://ko-fi.com/deaner?next=https://evil.example", False),
])
def test_support_url_allowlist(tmp_path, url, ok):
    from mailsift.app import support_url
    f = tmp_path / "support_url.txt"
    f.write_text(f"# comment\n{url}\n")
    assert (support_url(f) == url) is ok


def test_status_exposes_support_url(client, monkeypatch):
    monkeypatch.setattr("mailsift.app.support_url", lambda: "https://ko-fi.com/deaner")
    assert client.get(BASE + "/api/status").get_json()["support_url"] == "https://ko-fi.com/deaner"


def test_history_endpoint_returns_scans_and_actions(client):
    body = client.get(BASE + "/api/history").get_json()
    assert "items" in body and "scans" in body
    assert isinstance(body["scans"], list)
