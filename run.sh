#!/usr/bin/env sh
# MailSift launcher for macOS / Linux / Raspberry Pi. Pass --demo or --dry-run to try it safely first.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    echo "Setting up MailSift for the first time..."
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip >/dev/null
    .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python -m mailsift "$@"
