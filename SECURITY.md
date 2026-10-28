# MailSift security notes

MailSift has delegated read/write access to someone's mailbox, so it's built on the
assumption that every email it reads is hostile and that the PC it runs on is shared
with other software. This is what it defends against, and how.

## Assets

1. The user's Microsoft **refresh token**, which grants mailbox access.
2. The **sender list**: who emails them, which leaks a lot about a person.
3. The ability to **act** on the mailbox: send mail, move mail, create rules.

The user's **password** is not an asset MailSift ever holds. Sign-in happens on
Microsoft's own page (MSAL auth code + PKCE via the system browser; device-code sign-in is
deliberately unsupported and disabled on the app registration, as it's a common phishing vector),
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
| Destructive mistakes | Nothing is hard-deleted: purge moves mail to Deleted Items, block moves it to Junk. A confirm dialog lists every action, with blocks shown separately. Blocking a genuine-looking sender needs an explicit tick. The last clean-up can be **undone** (mail moved back to its original folder, blocks lifted). Never-touch senders are refused server-side. `--dry-run` and `--demo` modes exist. |
| Over-broad permissions | Only `User.Read`, `Mail.ReadWrite`, `Mail.Send` and `MailboxSettings.ReadWrite` are requested, each with a stated purpose. Message bodies are never requested: `$select` covers ids, sender, date, read flag, folder and headers only. |
| Personal data in logs | Per-request logging is off. The log holds lifecycle events and error types only. |
| Hostile `mailto:` unsubscribe headers making you send attacker-written email | The sender's `subject`/`body` are ignored. MailSift always sends the fixed text "unsubscribe" to exactly one validated address. |
| Access token leaking to a non-Microsoft host | Absolute URLs (Graph's `@odata.nextLink`) are only followed if they start with `https://graph.microsoft.com/v1.0/`. Anything else is refused before the token is attached. |
| Over-broad block rules | Rules match the exact sender (`fromAddresses`), not a substring, so blocking `info@x.com` can't also block `sales-info@x.com`. |
| Launch keys in logs | In the windowed `.exe` there's no console, so `--no-browser` never prints a key (it would land in the log). |
| Oversized local requests | Request bodies are capped at 512 KB. |
| Device-code phishing (a scammer starts a sign-in with MailSift's public client ID and tricks someone into typing the code) | Device-code sign-in is removed from the app, and "Allow public client flows" is switched off on the app registration, so Microsoft refuses it outright. |
| Supply chain | Every Python dependency, including build and test tools, is pinned to an exact version **and cryptographic hash** in `requirements.lock`, and installed with `pip --require-hashes`: a hijacked release on PyPI, or a tampered download, is rejected before it runs. GitHub Actions are pinned to full commit SHAs, so a moved or hijacked tag can't change what runs. The workflow token is read-only and checkout doesn't persist credentials. Every build runs the tests, **Bandit** (fails on medium+) and **pip-audit** (fails on known CVEs), and publishes a CycloneDX **SBOM** listing everything inside. **CodeQL** and **OpenSSF Scorecard** run on a schedule, with results in the Security tab. Dependabot raises updates weekly. |

## Tampering, copying and "theft"

Anything you hand to someone else's computer can, in the end, be copied or
unpacked. So MailSift's protection is built around **making tampering
obvious** and **having nothing worth stealing inside**:

- **Nothing secret is inside the .exe.** It contains no passwords, API keys or
  client secret. The client ID is public by design (it appears in every
  Microsoft sign-in URL). Unpacking the .exe gains an attacker nothing they
  couldn't see anyway.
- **Verify the real thing:** every build publishes a SHA-256 checksum
  (`MailSift.exe.sha256`, also shown on the GitHub Actions run page). Anyone
  can check their copy with
  `certutil -hashfile MailSift.exe SHA256` in a command prompt.
- **Provenance you can check:** official builds come with a signed
  build-provenance attestation, proving the exact .exe was built by this
  repo's GitHub workflow from a specific commit. With the GitHub CLI:
  `gh attestation verify MailSift.exe --repo deanobaker3-alt/mailsift`.
  The .exe's Properties → Details tab also carries the product name, version
  and copyright.
- **Only download official releases** from this repo's Releases page. A copy
  from anywhere else might be modified; the checksum and attestation will
  tell you.
- **See exactly what's inside:** every release ships `MailSift-sbom.json`, a
  CycloneDX parts list of every bundled component and version.
- **Licence:** dual-licensed (PolyForm Noncommercial + commercial, see
  `LICENSING.md`). Free copies must keep the copyright notice, and nobody but
  the copyright holder may sell it.
- **Kill switch:** if a tampered or impersonating copy appears, the publisher
  can disable or delete the app registration in Microsoft Entra, and every
  copy (real or fake) instantly stops being able to sign in. Then rebuild
  with a new registration.

Deliberately **not** done:
- **Code obfuscation (e.g. PyArmor).** It slows a determined reverser by hours,
  not days, and noticeably increases antivirus false positives on PyInstaller
  builds.
- **Self-checking the .exe's own hash.** A tamperer simply deletes the check.

What *would* help if MailSift goes public: an Authenticode **code-signing
certificate** (stops SmartScreen warnings and makes any modification break
the signature), and Microsoft **publisher verification**.

## Known limits

- **Malware already running as the user** can do what the user can: read DPAPI-protected
  data, read `launcher.secret`, drive the browser. No desktop app can fully defend against that.
- The `.exe` is **not code-signed**, so Windows SmartScreen warns about it. Note that
  signing alone doesn't remove the warning immediately: SmartScreen reputation builds
  over several releases signed with the same identity.
- The app registration is **unverified** (no Microsoft publisher verification), so
  Microsoft's consent screen says so.
- A spammer who owns a real domain can pass DMARC and appear "1-click". Unsubscribing
  from them confirms the address is live. MailSift never unsubscribes on its own;
  the user chooses every action, and **block** is always available.
- `isRead` stands in for "opened": anyone who marks all as read will show ~100% opened.
  Emails-per-month and the "noisy" filter don't depend on it.

## Reporting a vulnerability

Please **don't** open a public issue. Use GitHub's private reporting instead:
**Security tab → Report a vulnerability** on this repository. Include steps to
reproduce, but never real email content, addresses or tokens. You'll get a
reply, and credit in the release notes if you'd like it.
