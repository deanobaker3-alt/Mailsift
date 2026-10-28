# Building MailSift

Everything you need to register your own copy with Microsoft, build the `.exe`, and cut releases.

## 1. Register the app with Microsoft (once, about 10 minutes)

MailSift talks to Outlook.com through Microsoft Graph, so it needs an app registration. It's free.

1. **Get a directory.** Microsoft no longer lets personal accounts register apps outside one, so create a free personal Entra tenant, e.g. by signing up at **azure.microsoft.com/free** with your Hotmail. (Azure asks for a card as an identity check; app registrations aren't billable.)
2. In **entra.microsoft.com** (in that personal directory), go to **App registrations → New registration**:
   - **Supported account types:** *Personal Microsoft accounts only*
   - **Redirect URI:** *Public client/native (mobile & desktop)*, `http://localhost`
3. **Authentication → Allow public client flows: No.** MailSift uses browser sign-in (auth code + PKCE) only. Leaving this off blocks device-code phishing using your app's identity.
4. Copy the **Application (client) ID** from the Overview page.

You don't need a client secret (MailSift is a public client) or pre-added API permissions (users consent at sign-in). The client ID isn't secret either; it appears in every sign-in URL.

**Kill switch:** if a tampered or impersonating build ever appears, disable or delete the registration. Every copy stops being able to sign in at once.

## 2. Build

### With GitHub Actions (recommended)

Add these under **Settings → Secrets and variables → Actions → Variables**:

| Variable | Required | Example |
|---|---|---|
| `MAILSIFT_CLIENT_ID` | yes | `1a2b3c4d-…` |
| `MAILSIFT_SUPPORT_URL` | no | `https://ko-fi.com/yourname` (tip-jar button; only ko-fi.com, buymeacoffee.com, github.com/sponsors and paypal.me are accepted) |

Every push to `main` runs the tests, Bandit and pip-audit, builds `MailSift.exe` and uploads it as a workflow artifact with its SHA-256. On public repos it also adds a signed build-provenance attestation.

### Locally (Windows)

Put your client ID on its own line in `mailsift/client_id.txt` (and optionally a tip-jar link in `mailsift/support_url.txt`), then double-click **`build_exe.bat`**. It runs the same checks, then writes `dist\MailSift.exe`, its checksum and the user guide.

### From source (any OS, for development)

```
pip install -r requirements-build.txt bandit pip-audit
python -m mailsift --demo        # pretend mailbox, no sign-in
python -m mailsift --dry-run     # real mailbox, changes nothing
python -m pytest -q              # tests
```

Other options: `--no-browser` (print a one-time link), `--stay` (don't auto-exit when the tab closes), `--port`.

## 3. Releases

1. Bump `__version__` in `mailsift/__init__.py` (e.g. `1.6.0`) and push.
2. Tag it: `git tag v1.6.0 && git push origin v1.6.0`. (Or on GitHub: **Releases → Draft a new release → Choose a tag → v1.6.0**.)
3. The workflow checks the tag matches the version, builds, then publishes a GitHub Release with `MailSift.exe`, `MailSift.exe.sha256`, `USER_GUIDE.txt` and generated notes.

## 4. Repository settings worth having (public repo)

- **Settings → Code security:** turn on Dependabot alerts, secret scanning with push protection, CodeQL (*default setup*) and **private vulnerability reporting**.
- **Settings → Rules → Rulesets** (branch `main`): block force pushes and deletion, and require the *Build MailSift.exe* check to pass on pull requests.
- **Settings → Actions → General:** *Workflow permissions* set to read-only, and *Require approval for all external contributors* for fork pull requests.
- **Your account → Settings → Emails:** *Keep my email addresses private* and *Block command line pushes that expose my email*.

## Where things are kept on a user's PC

`%APPDATA%\MailSift` on Windows (`~/.mailsift` elsewhere):

| File | What |
|---|---|
| `token_cache.bin` | Microsoft sign-in, **encrypted by the OS**; never written if no encrypted store exists |
| `mailsift.db` | Who was unsubscribed from, blocked or protected; undo data; action log |
| `launcher.secret`, `instance.json` | Lets a second launch reuse the running copy (user-only) |
| `mailsift.log` | Start/stop and error types only; no addresses, content or tokens |

**Forget me** in the app deletes the token and wipes `mailsift.db`.

## Code layout

```
mailsift/
  auth.py       MSAL browser sign-in; token cache encrypted-or-memory-only
  graph.py      Microsoft Graph client: per-thread sessions, Retry-After back-off, $batch, token-leak guard
  headers.py    List-Unsubscribe / Authentication-Results / mailto parsing
  classify.py   one-click / mailto / web form / unverified / suspicious decisions
  scanner.py    parallel date-window scan, grouping, ignored-unsubscribe detection
  actions.py    unsubscribe, purge, block, undo
  blocklist.py  the merged "MailSift blocklist" inbox rule(s)
  netguard.py   SSRF-safe, IP-pinned one-click POST
  store.py      local SQLite: history, undo data, never-touch list
  launcher.py   single instance, port choice, launcher secret
  app.py        local Flask app: session lock, CSRF, CSP
  demo.py       pretend mailbox for --demo
  static/       the UI: retro arcade theme, original pixel art, bundled OFL fonts
tests/          unit + app tests (pytest)
tools/          build helpers
```
