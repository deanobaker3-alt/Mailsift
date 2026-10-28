# MailSift security notes

MailSift has delegated read/write access to someone's mailbox, so it's built on the
assumption that every email it reads is hostile and that the PC it runs on is shared
with other software. This is what it defends against, and how.

## Assets

1. The user's Microsoft **refresh token**, which grants mailbox access.
2. The **sender list**: who emails them, which leaks a lot about a person.
3. The ability to **act** on the mailbox: send mail, move mail, create rules.

The user's **password** is not an asset MailSift ever holds. Sign-in happens on
Microsoft's own page (MSAL auth code + PKCE via the system browser, or device code),
and MailSift only receives tokens.

## Threats and controls

| Threat | Control |
|---|---|
| Token theft from disk | The token cache uses the OS secret store (Windows DPAPI, macOS Keychain, Linux libsecret) via `msal-extensions`. If none is available it is **memory-only**; there is no plaintext fallback. *forget me* deletes it. |
| Other websites in the user's browser driving the local API (CSRF), or reading it | The API needs an HttpOnly `SameSite=Strict` session cookie **and**, for POSTs, an `X-MailSift-Token` header from the page. CORS isn't enabled, so cross-origin reads fail anyway. |
| DNS rebinding against `127.0.0.1` | Requests whose `Host` isn't `127.0.0.1:<port>` / `localhost:<port>` get a 403. |
| Other programs or users on the same PC reading the sender list | The session cookie is only issued against a **one-time launch key** (2-minute TTL) that MailSift hands to the browser it opens. A second launch gets a new key by proving it can read `launcher.secret`, which is private to the user. Without a key you get a locked page and 401s. |
| Clickjacking / framing | `X-Frame-Options: DENY`, CSP `frame-ancestors 'none'`. |
| XSS via hostile display names or addresses | All UI is built with `createElement`/`textContent`. CSP `script-src 'self'` with no inline script or style, so injected markup couldn't run anyway. |
| SSRF from hostile `List-Unsubscribe` URLs (pointing MailSift at your router, NAS or cloud metadata) | HTTPS on port 443 only, no userinfo. **Every** resolved address must be globally routable. The POST is then sent to that exact checked IP, with the real hostname used for SNI and certificate verification, so DNS can't change between check and connect. No redirects, cookies or credentials. |
| Unsubscribing from spammers (confirms the address is live) | Senders failing DMARC/compauth, unauthenticated senders, lookalike or brand-impersonating domains, spammy TLDs, raw-IP links and junk-heavy unauthenticated senders are **block-only**. This is enforced server-side, not just hidden in the UI. |
| Forged `Authentication-Results` added by the sender | Only the topmost header (stamped by Microsoft on receipt) is trusted. |
| Destructive mistakes | Nothing is hard-deleted: purge moves mail to Deleted Items, block moves it to Junk. A confirm dialog lists every action. `--dry-run` and `--demo` modes exist. |
| Over-broad permissions | Only `User.Read`, `Mail.ReadWrite`, `Mail.Send` and `MailboxSettings.ReadWrite` are requested, each with a stated purpose. Message bodies are never requested: `$select` covers ids, sender, date, read flag, folder and headers only. |
| Personal data in logs | Per-request logging is off. The log holds lifecycle events and error types only. |
| Supply chain | Dependencies are pinned to exact versions. The build script runs the test suite before packaging. |

## Known limits

- **Malware already running as the user** can do what the user can: read DPAPI-protected
  data, read `launcher.secret`, drive the browser. No desktop app can fully defend against that.
- The `.exe` is **not code-signed**, so Windows SmartScreen warns about it. Sign it if you
  distribute widely.
- The app registration is **unverified** (no Microsoft publisher verification), so
  Microsoft's consent screen says so.
- `isRead` stands in for "opened": anyone who marks all as read will show ~100% opened.
  Emails-per-month and the "noisy" filter don't depend on it.

## Reporting

Found a problem? Tell the person who gave you MailSift. Please don't open an issue
with real email data in it.
