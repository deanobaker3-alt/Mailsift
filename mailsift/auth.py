"""Sign-in to a personal Microsoft account (Outlook.com / Hotmail) with MSAL.

MailSift never sees a password: sign-in happens on Microsoft's own page, and
MailSift only receives tokens. To save you signing in every time it keeps
the refresh token, but only in an OS-encrypted store (Windows DPAPI, macOS
Keychain, Linux libsecret). If no encrypted store is available the token is
kept in memory only and forgotten when MailSift closes; it is never written
to disk in plain text.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import msal

log = logging.getLogger("mailsift")

AUTHORITY = "https://login.microsoftonline.com/consumers"

# Least privilege for what the app does:
#   Mail.ReadWrite            read headers, move old mail to Deleted Items / Junk
#   Mail.Send                 send "unsubscribe" emails for mailto-only senders
#   MailboxSettings.ReadWrite create the inbox rule that blocks a sender
#   User.Read                 show which account is signed in
SCOPES = ["User.Read", "Mail.ReadWrite", "Mail.Send", "MailboxSettings.ReadWrite"]


class NotSignedIn(Exception):
    pass


def _build_cache(path: Path):
    """Returns (cache, encrypted). Never falls back to a plaintext file."""
    try:
        from msal_extensions import PersistedTokenCache, build_encrypted_persistence
        path.parent.mkdir(parents=True, exist_ok=True)
        return PersistedTokenCache(build_encrypted_persistence(str(path))), True
    except Exception as exc:  # no OS secret store (e.g. a headless Linux box)
        log.warning("No encrypted token store available (%s); sign-in will be kept in memory only",
                    exc.__class__.__name__)
        return msal.SerializableTokenCache(), False


class Auth:
    def __init__(self, client_id: str, cache_path: Path):
        self.client_id = client_id
        self.cache_path = cache_path
        self.cache, self.cache_encrypted = _build_cache(cache_path)
        self._app = None
        self._lock = threading.Lock()
        self.flow = {"state": "idle"}

    @property
    def app(self):
        # Built on first use: MSAL contacts Microsoft when it's created, and the
        # app shouldn't crash on launch just because the network is down.
        if self._app is None:
            self._app = msal.PublicClientApplication(self.client_id, authority=AUTHORITY, token_cache=self.cache)
        return self._app

    # -- state -----------------------------------------------------------
    def account(self):
        accounts = self.app.get_accounts()
        return accounts[0] if accounts else None

    def username(self) -> str | None:
        try:
            account = self.account()
        except Exception:
            return None
        return account.get("username") if account else None

    def token(self) -> str:
        try:
            account = self.account()
            if not account:
                raise NotSignedIn("Not signed in")
            result = self.app.acquire_token_silent(SCOPES, account=account)
        except NotSignedIn:
            raise
        except Exception as exc:
            raise NotSignedIn(f"Couldn't reach Microsoft sign-in ({exc.__class__.__name__}). Check your connection.") from exc
        if not result or "access_token" not in result:
            raise NotSignedIn("Your sign-in has expired. Please sign in again.")
        return result["access_token"]

    def signed_in(self) -> bool:
        try:
            self.token()
            return True
        except NotSignedIn:
            return False

    # -- sign-in flows ---------------------------------------------------
    def _finish(self, result):
        with self._lock:
            if result and "access_token" in result:
                self.flow = {"state": "done"}
            else:
                message = (result or {}).get("error_description") or (result or {}).get("error") or "Sign-in failed"
                self.flow = {"state": "error", "message": message.splitlines()[0]}

    def start_interactive(self):
        """Opens the system browser on Microsoft's sign-in page."""
        with self._lock:
            if self.flow.get("state") == "pending":
                return self.flow
            self.flow = {"state": "pending", "mode": "browser"}

        def run():
            try:
                result = self.app.acquire_token_interactive(SCOPES, prompt="select_account", timeout=300)
            except Exception as exc:  # browser couldn't open, port blocked, etc.
                result = {"error": "interactive_failed", "error_description": str(exc)}
            self._finish(result)

        threading.Thread(target=run, daemon=True).start()
        return self.flow

    def logout(self):
        try:
            for account in self.app.get_accounts():
                self.app.remove_account(account)
        except Exception as exc:
            log.warning("Sign-out couldn't clear the token cache (%s)", exc.__class__.__name__)
        with self._lock:
            self.flow = {"state": "idle"}

    def forget(self):
        """Sign out and delete the token store from disk entirely."""
        self.logout()
        for path in (self.cache_path, self.cache_path.with_name(self.cache_path.name + ".lockfile")):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Couldn't delete %s: %s", path.name, exc.__class__.__name__)
