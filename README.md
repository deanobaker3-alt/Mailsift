# MailSift: subscription cleaner

Scans your Outlook.com / Hotmail mailbox for mailing lists, shows who sends what and how much of it you actually open, then unsubscribes from or blocks the ones you pick.

It runs entirely on your own computer. Your mail goes from Microsoft to your PC and nowhere else.

## What it does

- **Scan:** reads email *headers* (not bodies) for the last 12 months, 2 years or everything. Only mailing-list mail (anything with `List-Unsubscribe` or `List-Id`) appears. Personal email is never listed or touched.
- **Group:** one row per sender, with email count, emails per month, % opened, last sent, SPF/DKIM/DMARC verdicts, and how it can be unsubscribed. You can search (`grep>`), sort (most emails, most per month, least opened, most recent, name) and filter (opened <5%, noisy ≥4/mo, suspicious, ignored you, selected).
- **Act:** for each sender choose **keep**, **unsub**, **unsub+purge** or **block**. Or use the bulk buttons: **smart pick**, or apply an action to everything currently visible.

| Sender type | What "Unsubscribe" does |
|---|---|
| One-click (RFC 8058) | Sends the standard one-click request in the background. No web page is ever opened. |
| Unsubscribe by email | Sends a short "unsubscribe" email from your account to the address the sender lists. |
| Unsubscribe page | Gives you a button to open the sender's unsubscribe page yourself, since some need a click on their site. |
| Looks dodgy / couldn't verify | **Can't be unsubscribed, only blocked.** Unsubscribing from spam just confirms your address is live. |

- **Purge:** moves that sender's emails to **Deleted Items**.
- **Block:** creates an Outlook inbox rule (named `MailSift block: <address>`) that sends their future mail to Junk, and moves existing mail there too.
- **Nothing is ever permanently deleted.** Everything can be recovered in Outlook.
- **Follow-up:** MailSift remembers who you unsubscribed from. If a sender is still emailing you more than 3 days later, the next scan flags it as **Ignored your unsubscribe** so you can block it. **Smart pick** blocks them automatically.

### How "dodgy" is decided

A sender is block-only if any of these are true:

- It failed DMARC / Microsoft's composite authentication.
- It sends from a TLD favoured by spammers (`.top`, `.xyz` and friends).
- Its domain is a lookalike of a well-known brand (`amaz0n`, `paypa1`).
- The display name claims to be a brand it doesn't send from.
- The display name contains a different email address.
- The unsubscribe link points at a raw IP address.
- Outlook already junks most of its mail *and* it isn't authenticated.

Senders that don't pass DMARC, DKIM or Microsoft's compauth are "unverified" and block-only too. The reasons are shown on each row.

## How sharing works

There are two roles:

- **You (the publisher)** register MailSift with Microsoft **once**, put the resulting app ID in `mailsift/client_id.txt`, and build `MailSift.exe`.
- **Everyone else** just downloads `MailSift.exe`, double-clicks it, and signs in with their own Hotmail / Outlook.com / Live account. They get no setup screen, need no Azure account, and don't need Python.

Each person's copy runs on **their own computer**. Their mail and sign-in go from Microsoft to their PC and nowhere else, including not to you. You never hold anyone's tokens or data, so there's no server to run or secure, and you aren't a data processor for their mail.

**Accounts it works with:** personal Microsoft mailboxes (hotmail.com / hotmail.co.uk / outlook.com / live.com / msn.com). Gmail and work accounts can't sign in to it.

## Publisher setup (once, about 10 minutes)

1. **Get a directory to register the app in.** Microsoft no longer lets personal accounts register apps "outside a directory", so you need a free Entra tenant of your own:
   - Easiest route: sign up for a free Azure account at **https://azure.microsoft.com/free** with your Hotmail account. This creates a "Default Directory".
   - Azure asks for a card to verify identity. App registrations are free and nothing here is billable.
   - Use a personal tenant rather than your employer's. That keeps this fully separate from work, and the app still signs in to your Hotmail because of the account type below.
2. Go to **https://entra.microsoft.com**, make sure you're in that personal directory (top-right switcher), then **App registrations → New registration**:
   - Name: `MailSift` (anything you like).
   - Supported account types: **Personal Microsoft accounts only**.
   - Redirect URI: choose **Public client/native (mobile & desktop)** and enter `http://localhost`.
   - Click **Register**.
3. On the app's **Overview** page, copy the **Application (client) ID**.
4. *(Optional, for headless machines such as a Raspberry Pi)* Under **Authentication**, set **Allow public client flows** to **Yes**. This enables sign-in with a code.
5. You don't need to add API permissions by hand. MailSift asks for these when you sign in, and you approve them on Microsoft's consent screen:

| Permission | Why |
|---|---|
| `Mail.ReadWrite` | Read headers; move mail to Deleted Items or Junk |
| `Mail.Send` | Send "unsubscribe" emails for senders that only offer that |
| `MailboxSettings.ReadWrite` | Create the inbox rule that blocks a sender |
| `User.Read` | Show which account is signed in |

   No client secret is needed. MailSift is a "public client", the same as Outlook on your phone. The client ID isn't secret either (it appears in every sign-in URL), so it's fine to ship inside the app.
6. Open `mailsift/client_id.txt` and paste the client ID on its own line.
7. **Build the .exe** on a Windows PC: double-click `build_exe.bat`. It runs the tests, then builds `dist\MailSift.exe`: one file of about 25 MB, with the MailSift icon and no console window. Share that.
   - If you put the project on GitHub, `.github/workflows/build.yml` builds the .exe for you on every push (Actions tab → Artifacts).

### What your users will see

- **Windows SmartScreen:** "Windows protected your PC", because the .exe isn't code-signed. They click *More info → Run anyway*. Getting rid of this needs a code-signing certificate, which costs money each year.
- **Microsoft's consent screen** lists the permissions above and shows MailSift as an **unverified** app. Removing that label needs Microsoft *publisher verification*, which requires a Microsoft partner (Partner Center) account tied to a verified business domain. That's fine for friends and family; worth doing if this ever goes public.
- Test with a mate's Hotmail before sharing widely. Microsoft's consent rules for unverified apps do change from time to time.

## Running it

**Your users:** double-click `MailSift.exe`. There's no console window: their browser simply opens on MailSift.
- **To stop it:** click **quit** in the top-right of the page. If they just close the tab, MailSift shuts itself down about 10 minutes later.
- **Launching it again while it's running** reopens the browser tab instead of starting a second copy.
- If port 8765 is busy, it picks another free port automatically.

**You, from source:** you need **Python 3.10+**. Double-click `run.bat` on Windows, or run `./run.sh` on macOS/Linux.

### Try it safely first (recommended)

```
run.bat --demo        # pretend mailbox, no sign-in, nothing real happens
run.bat --dry-run     # real scan of your mailbox, but no unsubscribing, moving or blocking
```

The `.exe` takes the same options from a command prompt, for example `MailSift.exe --dry-run`.

Other options:
- `--device-code`: sign in with a code on another device.
- `--no-browser`: print a one-time link instead of opening the browser.
- `--stay`: don't auto-exit when the tab is closed.
- `--port 9000`

The top of every page shows the mode: **LIVE**, **DRY RUN** or **DEMO**.

## Where things are kept

Everything lives in `%APPDATA%\MailSift` on Windows and `~/.mailsift` elsewhere.

| File | What | Sensitive? |
|---|---|---|
| `token_cache.bin` | Your Microsoft sign-in (refresh token), so you needn't sign in every time | Yes, so it's **encrypted by the OS** (DPAPI / Keychain / libsecret). If no encrypted store exists, it's never written at all and kept in memory only. |
| `mailsift.db` | Who you unsubscribed from and blocked, plus an action log | Sender addresses only, no email content |
| `launcher.secret`, `instance.json` | Lets a second launch reuse the running copy | Readable by your user only |
| `mailsift.log` | Start/stop and errors, small and rotated | No tokens, addresses or email content |
| `config.json` | Only if someone pasted a client ID into the setup screen | No |

**Your password is never stored anywhere.** You type it on Microsoft's own sign-in page, and MailSift only ever receives tokens.

**forget me** (on the scan screen) signs you out, deletes `token_cache.bin`, and wipes `mailsift.db`. To also revoke MailSift's access on Microsoft's side, go to https://account.live.com/consent/Manage.

## Security

See **SECURITY.md** for the threat model, every control, and how tampering is handled.

**Verify a download:** each build ships `MailSift.exe.sha256`. In a command prompt:

```
certutil -hashfile MailSift.exe SHA256
```

The output should match the hash in the `.sha256` file and on the GitHub Actions run page.

## Licence

Proprietary, all rights reserved. See `LICENSE`.

## Tests

```
pip install -r requirements-build.txt
python -m pytest
```

62 tests cover:
- header parsing and classification;
- the SSRF/rebinding guard and the Graph token-leak guard;
- parallel-scan correctness;
- every action path, including dry run;
- the launch-key/session lock, CSRF, host checks, quit and forget-me.

## Layout

```
mailsift/
  auth.py       MSAL sign-in; token cache encrypted-or-memory-only
  graph.py      Microsoft Graph client: per-thread sessions, Retry-After back-off, $batch
  headers.py    List-Unsubscribe / Authentication-Results / mailto parsing
  classify.py   one-click / mailto / web form / unverified / suspicious decisions
  scanner.py    parallel date-window scan, grouping, ignored-unsubscribe detection
  actions.py    unsubscribe, purge, block (inbox rule)
  netguard.py   SSRF-safe, IP-pinned one-click POST
  store.py      local SQLite history (+ wipe)
  launcher.py   single instance, port choice, launcher secret
  app.py        local Flask app: session lock, CSRF, CSP
  demo.py       fake mailbox for --demo
  static/       the UI (no inline scripts or styles; CSP-clean)
```
