"""The local web app.

Who can talk to it:
  * It listens on 127.0.0.1 only, and rejects any request whose Host header
    isn't this machine (defeats DNS-rebinding tricks).
  * MailSift opens your browser with a one-time launch key. Swapping that key
    sets an HttpOnly, SameSite=Strict session cookie. Every API call needs
    that cookie, so another website, another program, or another user on the
    same PC can't read your sender list or trigger actions.
  * Every POST additionally needs a per-launch anti-CSRF token from the page.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_from_directory

from . import __version__
from .actions import Actions
from .auth import Auth, NotSignedIn
from .graph import GraphClient, GraphError
from .scanner import SCOPES, Scanner
from .store import Store

log = logging.getLogger("mailsift")

STATIC = Path(__file__).parent / "static"
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
LAUNCH_KEY_TTL = 120  # seconds a one-time launch key stays valid
BUNDLED_ID_FILE = Path(__file__).parent / "client_id.txt"
SUPPORT_FILE = Path(__file__).parent / "support_url.txt"
# Only well-known tip-jar sites, so a tampered build can't turn the button into a phishing link.
# Looks, chosen from the switcher in the header. Whitelisted: the value is
# written into the page's <html> tag, so nothing else may ever get there.
THEMES = ("retro", "ops", "blueprint")
DEFAULT_THEME = "retro"
SUPPORT_URL = re.compile(r"^https://(ko-fi\.com|(www\.)?buymeacoffee\.com|github\.com/sponsors|paypal\.me)/[A-Za-z0-9_-]{1,64}/?$")


def support_url(path: Path = SUPPORT_FILE) -> str | None:
    """The publisher's optional "buy me a coffee" link. Lines starting with # are ignored."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and SUPPORT_URL.match(line):
                return line
    except OSError:
        pass
    return None


def bundled_client_id(path: Path = BUNDLED_ID_FILE) -> str | None:
    """The publisher's Application (client) ID, shipped with the app.

    A public client's ID isn't a secret (it's visible in every sign-in URL), so
    it's fine to distribute. Lines starting with # are ignored.
    """
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and GUID.match(line):
                return line
    except OSError:
        pass
    return None


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class AppState:
    def __init__(self, data_dir: Path, demo=False, dry_run=False):
        self.data_dir = data_dir
        self.demo = demo
        self.dry_run = dry_run
        self.lock = threading.Lock()
        self._config_lock = threading.Lock()
        self.scan_job = {"state": "idle", "log": []}
        self.apply_job = {"state": "idle"}
        self.undo_job = {"state": "idle"}
        self.scan_cancel = threading.Event()
        self.last_scan = None
        self.last_seen = time.monotonic()
        self.auth = self.graph = None
        saved = self.config().get("theme") if not demo else None
        self.theme = saved if saved in THEMES else DEFAULT_THEME
        self.poster = None

        if demo:
            from .demo import DemoAuth, DemoGraph, demo_poster, seed_demo_store
            self.store = Store(":memory:")
            seed_demo_store(self.store)
            self.auth, self.graph, self.poster = DemoAuth(), DemoGraph(), demo_poster
        else:
            data_dir.mkdir(parents=True, exist_ok=True)
            self.store = Store(data_dir / "mailsift.db")
            # A personal override wins; otherwise use the ID the publisher built in,
            # so ordinary users never see the setup screen.
            client_id = self.config().get("client_id") or bundled_client_id()
            if client_id:
                self._init_auth(client_id)

    # -- configuration ---------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    def _write_config(self, cfg: dict):
        """Written to a temporary file and moved into place, so a crash or a
        flat battery mid-write can't leave a truncated config behind: that
        would read as empty and lose the user's app ID."""
        with self._config_lock:
            temp = self.config_path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            os.replace(temp, self.config_path)

    def config(self) -> dict:
        try:
            return json.loads(self.config_path.read_text())
        except (OSError, ValueError):
            return {}

    def save_theme(self, theme: str):
        if theme not in THEMES:
            raise ValueError("unknown theme")
        self.theme = theme
        if self.demo:
            return   # demo mode writes nothing to disk
        cfg = self.config()
        cfg["theme"] = theme
        self._write_config(cfg)

    def save_client_id(self, client_id: str):
        cfg = self.config()
        cfg["client_id"] = client_id
        self._write_config(cfg)
        self._init_auth(client_id)

    def _init_auth(self, client_id):
        self.auth = Auth(client_id, self.data_dir / "token_cache.bin")
        self.graph = GraphClient(self.auth.token, on_event=self.scan_log)

    def actions(self) -> Actions:
        if self.poster:
            return Actions(self.graph, self.store, dry_run=self.dry_run, poster=self.poster)
        return Actions(self.graph, self.store, dry_run=self.dry_run)

    # -- liveness ----------------------------------------------------------
    def scan_log(self, message: str):
        lines = self.scan_job.setdefault("log", [])
        lines.append(f"[{_stamp()}] {message}")
        del lines[:-60]

    def busy(self) -> bool:
        auth_pending = bool(self.auth) and self.auth.flow.get("state") == "pending"
        return (self.scan_job.get("state") == "running" or self.apply_job.get("state") == "running"
                or self.undo_job.get("state") == "running" or auth_pending)

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_seen


def _friendly(exc: Exception) -> str:
    if isinstance(exc, NotSignedIn):
        return str(exc)
    if isinstance(exc, GraphError):
        if exc.status in (401, 403):
            return "Microsoft refused access. Try signing out and in again."
        return f"Microsoft Graph error ({exc.status}): {exc.message}"
    return f"Something went wrong ({exc.__class__.__name__}). Details are in mailsift.log."


def create_app(data_dir: Path, port=8765, demo=False, dry_run=False,
               launcher_secret: str | None = None, shutdown=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024  # the UI never sends more than a few KB
    st = AppState(data_dir, demo=demo, dry_run=dry_run)
    session_secret = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    cookie_name = f"mailsift_{port}"  # cookies ignore ports, so keep instances apart
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    launch_keys: dict[str, float] = {}
    keys_lock = threading.Lock()
    shutdown = shutdown or (lambda: None)

    def new_launch_key() -> str:
        key = secrets.token_urlsafe(24)
        with keys_lock:
            now = time.monotonic()
            for k, expiry in list(launch_keys.items()):
                if expiry < now:
                    del launch_keys[k]
            launch_keys[key] = now + LAUNCH_KEY_TTL
        return key

    def consume_launch_key(key: str) -> bool:
        with keys_lock:
            expiry = launch_keys.pop(key, None)
        return expiry is not None and expiry >= time.monotonic()

    def has_session() -> bool:
        return hmac.compare_digest(request.cookies.get(cookie_name, ""), session_secret)

    app.config.update(MAILSIFT_STATE=st, MAILSIFT_TOKEN=csrf_token, MAILSIFT_NEW_LAUNCH_KEY=new_launch_key)

    # -- gatekeeping -----------------------------------------------------
    @app.before_request
    def guard():
        if request.host not in allowed_hosts:
            abort(403)
        path = request.path
        if path == "/api/launch-key":
            sent = request.headers.get("X-Launcher-Secret", "")
            if request.method != "POST" or not launcher_secret or not hmac.compare_digest(sent, launcher_secret):
                abort(403)
            return None
        if path.startswith("/api/"):
            if not has_session():
                return jsonify(error="locked", message="Open MailSift from its icon to use it."), 401
            if request.method == "POST" and not hmac.compare_digest(request.headers.get("X-MailSift-Token", ""), csrf_token):
                abort(403)
            st.last_seen = time.monotonic()
        return None

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.path.startswith("/api/") or request.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(403)
    def forbidden(_):
        return jsonify(error="forbidden", message="Blocked by MailSift's local security checks"), 403

    # -- pages -------------------------------------------------------------
    @app.get("/")
    def index():
        key = request.args.get("k")
        if key:
            if consume_launch_key(key):
                response = redirect("/", code=303)  # drop the key from the address bar and history
                response.set_cookie(cookie_name, session_secret, httponly=True, samesite="Strict", path="/")
                return response
            return send_from_directory(STATIC, "locked.html"), 403
        if not has_session():
            return send_from_directory(STATIC, "locked.html")
        st.last_seen = time.monotonic()
        html = (STATIC / "index.html").read_text(encoding="utf-8").replace("__MAILSIFT_TOKEN__", csrf_token)
        # Set before the first paint, so the page never flashes the wrong look.
        theme = st.theme if st.theme in THEMES else DEFAULT_THEME
        html = html.replace('<html lang="en-GB">', f'<html lang="en-GB" data-theme="{theme}">', 1)
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(STATIC, name)

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(STATIC, "icon.png")

    # -- launcher handshake (second launch reuses this instance) ----------
    @app.post("/api/launch-key")
    def launch_key():
        return jsonify(key=new_launch_key())

    # -- status, setup, lifecycle -----------------------------------------
    @app.get("/api/status")
    def status():
        configured = st.auth is not None
        signed_in = bool(configured and st.auth.signed_in())
        return jsonify(
            version=__version__,
            configured=configured,
            signed_in=signed_in,
            user=st.auth.username() if signed_in else None,
            auth=st.auth.flow if configured else {"state": "idle"},
            demo=st.demo,
            dry_run=st.dry_run,
            token_store=("encrypted" if getattr(st.auth, "cache_encrypted", False) else "memory-only") if configured else None,
            support_url=support_url(),
            theme=st.theme,
            scan_state=st.scan_job.get("state"),
            apply_state=st.apply_job.get("state"),
            undo_state=st.undo_job.get("state"),
            undo=(st.store.last_undoable() if signed_in and not st.dry_run else None),
            has_results=st.last_scan is not None,
        )

    @app.get("/api/ping")
    def ping():
        return jsonify(ok=True)

    @app.post("/api/quit")
    def quit_app():
        log.info("Quit requested from the browser")
        threading.Timer(0.4, shutdown).start()
        return jsonify(ok=True)

    @app.post("/api/forget")
    def forget():
        if st.busy():
            return jsonify(error="busy", message="Wait for the current scan or clean-up to finish first."), 409
        if st.auth:
            st.auth.forget()
        st.store.wipe()
        with st.lock:
            st.last_scan = None
            st.scan_job = {"state": "idle", "log": []}
            st.apply_job = {"state": "idle"}
            st.undo_job = {"state": "idle"}
        log.info("Forget-me: sign-in and history wiped")
        return jsonify(ok=True)

    @app.post("/api/config")
    def set_config():
        if st.demo:
            return jsonify(ok=True)
        client_id = str((request.get_json(silent=True) or {}).get("client_id", "")).strip()
        if not GUID.match(client_id):
            return jsonify(error="bad_client_id",
                           message="That doesn't look like an Application (client) ID. It's a GUID like 1a2b3c4d-…"), 400
        st.save_client_id(client_id)
        return jsonify(ok=True)

    @app.post("/api/login")
    def login():
        if st.auth is None:
            return jsonify(error="not_configured", message="This copy of MailSift has no app ID yet"), 400
        # Browser sign-in only (auth code + PKCE). Device-code sign-in is deliberately not
        # supported: it's a common phishing trick, and the app registration disables it.
        flow = st.auth.start_interactive()
        return jsonify(flow)

    @app.post("/api/logout")
    def logout():
        if st.auth:
            st.auth.logout()
        with st.lock:
            st.last_scan = None
            st.scan_job = {"state": "idle", "log": []}
            st.apply_job = {"state": "idle"}
        return jsonify(ok=True)

    # -- scan --------------------------------------------------------------
    @app.post("/api/scan")
    def start_scan():
        scope = (request.get_json(silent=True) or {}).get("scope", "12m")
        if scope not in SCOPES:
            return jsonify(error="bad_scope", message="Unknown scan range"), 400
        if not (st.auth and st.auth.signed_in()):
            return jsonify(error="not_signed_in", message="Please sign in first"), 401
        with st.lock:
            if st.scan_job.get("state") == "running":
                return jsonify(st.scan_job)
            st.scan_cancel.clear()
            st.scan_job = {"state": "running", "scope": scope, "checked": 0, "found": 0, "total": None,
                           "started": time.time(), "log": []}
        st.scan_log(f"scan started: {scope}, {Scanner(st.graph, st.store).workers} workers")

        def progress(checked, found, total):
            st.scan_job.update(checked=checked, found=found, total=total)

        def run():
            try:
                result = Scanner(st.graph, st.store).scan(scope, progress=progress,
                                                          cancelled=st.scan_cancel.is_set, log=st.scan_log)
                with st.lock:
                    if result.cancelled:
                        st.scan_job.update(state="cancelled")
                    else:
                        st.last_scan = result
                        st.scan_job.update(state="done", checked=result.checked, found=len(result.senders))
                        st.store.record_scan(result.scope, result.checked, len(result.senders),
                                             result.other_senders, st.dry_run)
                st.scan_log(f"scan {'cancelled' if result.cancelled else 'complete'}: "
                            f"{result.checked:,} emails, {len(result.senders)} mailing lists")
            except Exception as exc:  # report to the UI rather than dying silently
                log.exception("Scan failed")
                st.scan_job.update(state="error", message=_friendly(exc))

        threading.Thread(target=run, daemon=True, name="scan").start()
        return jsonify(st.scan_job)

    @app.post("/api/scan/cancel")
    def cancel_scan():
        st.scan_cancel.set()
        return jsonify(ok=True)

    @app.get("/api/scan")
    def scan_status():
        job = dict(st.scan_job)
        if job.get("state") in ("done", "idle", "cancelled") and st.last_scan is not None:
            job.update(state="done", scope=st.last_scan.scope, senders=st.last_scan.senders,
                       checked=st.last_scan.checked, other_senders=st.last_scan.other_senders,
                       other_messages=st.last_scan.other_messages, sent_checked=st.last_scan.sent_checked)
        return jsonify(job)

    # -- apply -------------------------------------------------------------
    @app.post("/api/apply")
    def start_apply():
        choices = (request.get_json(silent=True) or {}).get("choices") or {}
        if not isinstance(choices, dict) or not choices:
            return jsonify(error="nothing_chosen", message="Nothing chosen"), 400
        choices = {str(k): str(v) for k, v in choices.items()}
        if st.last_scan is None:
            return jsonify(error="no_scan", message="Scan your mailbox first"), 400
        with st.lock:
            if st.apply_job.get("state") == "running":
                return jsonify(st.apply_job)
            if st.undo_job.get("state") == "running":
                return jsonify(error="busy", message="Wait for the undo to finish first"), 409
            st.apply_job = {"state": "running", "done": 0, "results": [], "dry_run": st.dry_run,
                            "total": sum(1 for v in choices.values() if v != "keep")}
        scan = st.last_scan

        def progress(done, total, result=None):
            st.apply_job.update(done=done, total=total)
            if result is not None:
                st.apply_job["results"].append(result)

        def run():
            try:
                st.actions().apply(choices, scan, progress=progress)
                st.apply_job["state"] = "done"
            except Exception as exc:
                log.exception("Clean-up failed")
                st.apply_job.update(state="error", message=_friendly(exc))

        threading.Thread(target=run, daemon=True, name="apply").start()
        return jsonify(st.apply_job)

    @app.get("/api/apply")
    def apply_status():
        return jsonify(st.apply_job)

    # -- never-touch list -------------------------------------------------
    @app.post("/api/protect")
    def protect():
        body = request.get_json(silent=True) or {}
        sid, on = str(body.get("id", "")), bool(body.get("on"))
        scan = st.last_scan
        sender = scan.private.get(sid) if scan else None
        if sender is None:
            return jsonify(error="unknown_sender", message="Scan again, then try that"), 400
        st.store.set_protected(sender["address"], on)
        for s in scan.senders:
            if s["id"] == sid:
                s["protected"] = on
        return jsonify(ok=True, protected=on)

    # -- undo ----------------------------------------------------------------
    @app.post("/api/undo")
    def start_undo():
        if st.dry_run:
            return jsonify(error="dry_run", message="Nothing to undo in a dry run"), 400
        run_id = (request.get_json(silent=True) or {}).get("run_id")
        last = st.store.last_undoable()
        if not last or last["run_id"] != run_id:
            return jsonify(error="stale", message="That clean-up can't be undone any more"), 409
        with st.lock:
            if st.busy():
                return jsonify(error="busy", message="Wait for the current job to finish first"), 409
            st.undo_job = {"state": "running", "done": 0, "total": 1}

        def progress(done, total):
            st.undo_job.update(done=done, total=total)

        def run():
            try:
                summary = st.actions().undo(run_id, progress=progress)
                with st.lock:
                    st.last_scan = None  # message ids changed: the old scan is stale
                st.undo_job = {"state": "done", **summary}
            except Exception as exc:
                log.exception("Undo failed")
                st.undo_job = {"state": "error", "message": _friendly(exc)}

        threading.Thread(target=run, daemon=True, name="undo").start()
        return jsonify(st.undo_job)

    @app.get("/api/undo")
    def undo_status():
        return jsonify(st.undo_job)

    @app.post("/api/prefs")
    def prefs():
        theme = (request.get_json(silent=True) or {}).get("theme")
        if theme not in THEMES:
            return jsonify(error="bad_theme", message="Unknown theme"), 400
        st.save_theme(theme)
        return jsonify(ok=True, theme=theme)

    @app.post("/api/diagnostics")
    def diagnostics():
        # Graph allows only a few requests at once per mailbox, and a scan or
        # clean-up is already using them.
        if st.busy():
            return jsonify(error="busy", message="Wait for the current scan or clean-up to finish first."), 409
        from .diagnostics import run
        report = run(st.graph, st.auth, st.store, demo=st.demo)
        log.info("Diagnostics: %s problem(s) in %s checks", report["problems"], len(report["checks"]))
        return jsonify(report)

    @app.get("/api/history")
    def history():
        return jsonify(items=st.store.history(), scans=st.store.scans())

    return app
