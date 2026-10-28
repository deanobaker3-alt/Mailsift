# Contributing

Thanks for wanting to help! Bug reports, ideas and pull requests are all welcome.

## Before you start

- **Bugs:** open an issue saying what you did, what you expected and what happened. **Never paste real email content, addresses or tokens.** Use `--demo` to reproduce if you can.
- **Security problems:** please don't open a public issue; see [SECURITY.md](SECURITY.md).
- **Bigger changes:** open an issue first so we can agree the approach.

## The licensing bit (please read)

MailSift is dual-licensed (see [LICENSING.md](LICENSING.md)): free under PolyForm Noncommercial, and commercially by the copyright holder. So that every contribution can go into both editions:

> **By submitting a contribution, you license it to Dean Baker under the [MIT License](https://opensource.org/license/mit), and you confirm you have the right to do so** (it's your own work, or you have permission).

Your contribution stays credited to you in the git history.

## Making changes

```
pip install -r requirements-build.txt bandit pip-audit
python -m pytest -q                 # all tests must pass
python -m bandit -r mailsift -ll    # no medium/high findings
python -m mailsift --demo           # try it with the pretend mailbox
```

Ground rules, because this app handles people's mailboxes:

- Nothing leaves the user's PC except calls to Microsoft and the sender's own unsubscribe endpoint.
- Never permanently delete mail.
- Render anything from an email with `textContent`, never as HTML.
- New behaviour comes with tests.
