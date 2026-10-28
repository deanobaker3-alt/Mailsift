"""Single-instance handling, port choice and the launcher secret.

When MailSift is already running and you launch it again, the new copy asks
the running one for a fresh one-time launch key (proving itself with a secret
file only your user account can read), opens the browser, and exits.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
from pathlib import Path

import requests

SECRET_FILE = "launcher.secret"
INSTANCE_FILE = "instance.json"


def _write_private(path: Path, text: str):
    """Create a file readable only by the current user (POSIX 0600; on Windows,
    %APPDATA% is already private to the user)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def launcher_secret(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / SECRET_FILE
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value
    except OSError:
        pass
    value = secrets.token_urlsafe(32)
    _write_private(path, value)
    return value


def running_instance_key(data_dir: Path, secret: str):
    """(port, launch_key) from an already-running MailSift, else None."""
    try:
        info = json.loads((data_dir / INSTANCE_FILE).read_text(encoding="utf-8"))
        port = int(info["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    try:
        with requests.Session() as session:
            session.trust_env = False  # never route this via a proxy
            response = session.post(f"http://127.0.0.1:{port}/api/launch-key",
                                    headers={"X-Launcher-Secret": secret}, timeout=2)
        if response.ok:
            return port, response.json()["key"]
    except (requests.RequestException, ValueError, KeyError):
        pass
    return None


def pick_port(preferred: int) -> int:
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
                return probe.getsockname()[1]
            except OSError:
                continue
    raise OSError("No free local port")


def record_instance(data_dir: Path, port: int):
    _write_private(data_dir / INSTANCE_FILE, json.dumps({"port": port, "pid": os.getpid()}))


def clear_instance(data_dir: Path):
    try:
        info = json.loads((data_dir / INSTANCE_FILE).read_text(encoding="utf-8"))
        if info.get("pid") == os.getpid():
            (data_dir / INSTANCE_FILE).unlink()
    except (OSError, ValueError):
        pass
