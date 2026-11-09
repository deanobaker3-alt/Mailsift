# Licensing

MailSift is **dual-licensed**. Pick the one that fits.

## 1. Free: PolyForm Noncommercial 1.0.0

For **personal and non-commercial use**: cleaning up your own mailbox, your family's, learning from the code, hacking on it, forking it, sharing improved versions for free. Charities, schools, public bodies and other non-commercial organisations are covered too.

The full terms are in [`LICENSE.md`](LICENSE.md). In short:

| You can | You can't (without a commercial licence) |
|---|---|
| Use it for yourself, friends and family | Sell it, or sell a modified version |
| Read, change and fork the code | Bundle it into a paid product or service |
| Share copies and changes for free, keeping the notice | Use it inside a business for business purposes |

## 2. Commercial licence

Want to sell it, rebrand it, bundle it, or use it at work? A commercial licence is available from the copyright holder, **Dean Baker**. Open an issue titled **"Commercial licence"** on this repository (or message via [Ko-fi](https://ko-fi.com/tlemon)) and we'll sort something out.

## Third-party components

MailSift.exe bundles Python, Flask, MSAL, requests, urllib3, certifi and their dependencies, which stay under their own (permissive) licences. Each release also ships `MailSift-sbom.json`, a CycloneDX parts list of every Python component and its version.

Four typefaces are bundled, all under the **SIL Open Font License 1.1**, unmodified:

| Font | Used by | Licence |
|---|---|---|
| Press Start 2P | retro theme | [`OFL.txt`](mailsift/static/fonts/OFL.txt) |
| VT323 | retro theme | [`OFL.txt`](mailsift/static/fonts/OFL.txt) |
| IBM Plex Sans / Mono | ops console theme | [`OFL-ibm-plex.txt`](mailsift/static/fonts/OFL-ibm-plex.txt) |
| B612 / B612 Mono | blueprint theme | [`OFL-b612.txt`](mailsift/static/fonts/OFL-b612.txt) |

*This page is a plain-English summary; `LICENSE.md` is what actually applies.*
