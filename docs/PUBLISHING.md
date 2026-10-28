# Publishing checklist

Everything needed to take MailSift from this private repo to a public one.
Nothing here is urgent; work through it when you're ready.

---

## 1. Prove it works on real mailboxes

These two were written against a simulated mailbox and have **never been
confirmed against a real Outlook.com account**. Do these first.

- [ ] **Block rule.** Block one sender, then open **Outlook → Settings → Mail → Rules**.
      There should be exactly one rule named **MailSift blocklist**, containing that
      sender's address. (If you blocked anyone with v1.0–v1.2, their old one-per-sender
      rules should have been folded in and removed.)
- [ ] **Undo.** Do a small clean-up (one sender, `unsub+purge`), then **undo last clean-up**.
      The emails should return to the folder they came from. Outlook gives a message a new
      ID when it moves, which MailSift tracks; this is the bit most likely to misbehave.

## 2. Let other people break it

- [ ] Two or three friends run it on their own Hotmail for about a week.
- [ ] Watch for: sign-in problems on other account types, senders classified oddly,
      anything confusing in the wording.

## 3. Decide the open questions

- [ ] **Your name.** `LICENSE.md`, `LICENSING.md`, `CONTRIBUTING.md` and the `.exe`'s
      file properties all say **Dean Baker**. Normal for copyright and good for trust,
      but it becomes public and tied to the `deanobaker3-alt` account. Change
      `OWNER` in `tools/make_version_info.py` and those three files if you'd rather not.
- [ ] **Licence.** Currently free for personal use, commercial rights reserved
      (see `LICENSING.md`). Swap for MIT if you'd rather people could do anything with it.
- [ ] **Separate app registration.** Register a second Microsoft app for public
      releases and put its ID in `MAILSIFT_CLIENT_ID`, keeping your current one for
      personal builds. If the public one is ever abused you can disable it without
      locking yourself out. See `BUILDING.md` § 1.

## 4. Deal with the git history

⚠️ **The current `main` history contains your personal Gmail address**, in the merge
commit from Dependabot pull request #1. Publishing this repo as-is exposes it to
anyone who clones it or fetches the patch.

Two ways round it:

**Option A — fresh repo (cleanest).** The branch
**`clean-history-for-publishing`** in this repo is the identical codebase with that
commit removed and every commit re-signed. Create a new public repo and push that
branch to it as `main`. Pull request #1 and its commit stay behind in this private repo.

**Option B — force-push over `main`.** Quicker, but GitHub keeps commits attached to
pull request #1 reachable forever, so the address stays discoverable. Only do this if
you decide you don't mind.

Either way, turn on **Settings → Emails → Keep my email addresses private** and
**Block command line pushes that expose my email** on your GitHub account first.

## 5. Flip the switch

- [ ] **Settings → General → Change visibility → Public.**
- [ ] **Settings → Code security:** enable Dependabot alerts, secret scanning with
      push protection, and **private vulnerability reporting**. Leave CodeQL
      *default setup* **off** (the workflow here already does it).
- [ ] CodeQL and Scorecard start running by themselves once public: they currently
      skip on purpose. Check both go green.
- [ ] **Settings → Rules → Rulesets** on `main`: block force pushes and deletion,
      and require the *Build MailSift.exe* check on pull requests.
- [ ] **Settings → Actions → General:** workflow permissions read-only, and
      require approval for fork pull requests from outside contributors.

## 6. Cut the first public release

```
# bump __version__ in mailsift/__init__.py first, e.g. 1.7.0
git tag v1.7.0
git push origin v1.7.0
```

The workflow checks the tag matches the version, builds, attaches a signed
build-provenance attestation (public repos only), and publishes a Release with
`MailSift.exe`, its SHA-256, the user guide and the SBOM.

- [ ] Check the release page looks right, and that
      `gh attestation verify MailSift.exe --repo <owner>/<repo>` passes.
- [ ] Update the badge and download links at the top of `README.md` if the repo
      name or owner changed.

## Known limitations to be upfront about

Already documented in `SECURITY.md`, but expect questions:

- The `.exe` isn't code-signed, so SmartScreen warns. Signing doesn't fix this
  instantly either; reputation builds over several signed releases. Microsoft's
  cheap signing service is **US/Canada individuals only** — in the UK you'd need a
  registered company or a traditional certificate.
- Microsoft's consent screen calls the app **unverified** until you complete
  publisher verification.
- Windows only, and personal Microsoft accounts only.
