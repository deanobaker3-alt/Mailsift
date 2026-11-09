#!/usr/bin/env sh
# MailSift launcher for macOS / Linux / Raspberry Pi. Pass --demo or --dry-run to try it safely first.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    echo "Setting up MailSift for the first time..."
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip >/dev/null
    # The hash-pinned lock, not requirements.txt: SECURITY.md promises every
    # dependency is installed with --require-hashes, and this is the script
    # most people run. Runtime deps only; the build and test tools live in
    # requirements.lock.
    .venv/bin/python -m pip install --require-hashes -r requirements-run.lock
fi
exec .venv/bin/python -m mailsift "$@"
