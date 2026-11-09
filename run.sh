#!/usr/bin/env sh
# MailSift launcher for macOS / Linux / Raspberry Pi. Pass --demo or --dry-run to try it safely first.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    echo "Setting up MailSift for the first time..."
    python3 -m venv .venv
    # No "pip install --upgrade pip" here on purpose. That fetches pip
    # itself, unpinned, over the network -- and pip is the thing that then
    # enforces --require-hashes on everything else, so it is the worst
    # single package to leave unpinned. Every venv since Python 3.9 ships
    # a pip new enough for hash checking.
    # The hash-pinned lock, not requirements.txt: SECURITY.md promises every
    # dependency is installed with --require-hashes, and this is the script
    # most people run. Runtime deps only; the build and test tools live in
    # requirements.lock.
    .venv/bin/python -m pip install --require-hashes -r requirements-run.lock
fi
exec .venv/bin/python -m mailsift "$@"
