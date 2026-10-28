"""Start MailSift:  python -m mailsift [--demo] [--dry-run] [--port 8765] [--stay]"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__

IDLE_EXIT_SECONDS = 10 * 60  # quit by itself once the tab has been closed this long


def default_data_dir() -> Path:
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "MailSift"
    return Path.home() / ".mailsift"


HAS_CONSOLE = sys.stdout is not None  # False in the windowed .exe


def setup_logging(data_dir: Path) -> Path:
    """Errors and lifecycle events go to mailsift.log (small, rotated).
    Request lines aren't logged, and nothing personal is written there."""
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "mailsift.log"
    handler = RotatingFileHandler(log_path, maxBytes=256_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # no per-request spam
    for noisy in ("msal", "urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # A windowed .exe has no console: give stray prints somewhere harmless to go.
    if sys.stdout is None or sys.stderr is None:
        stream = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives for the process
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream
    return log_path


def fatal(message: str):
    logging.getLogger("mailsift").error(message)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "MailSift", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mailsift", description="MailSift subscription cleaner")
    parser.add_argument("--demo", action="store_true", help="use a fake mailbox; nothing real is touched")
    parser.add_argument("--dry-run", action="store_true", help="scan for real, but don't unsubscribe, move or block anything")
    parser.add_argument("--port", type=int, default=8765, help="preferred local port (another is picked if busy)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser; print the link instead")
    parser.add_argument("--stay", action="store_true", help="keep running even after the browser tab is closed")
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    args = parser.parse_args(argv)

    data_dir = args.data_dir / "demo" if args.demo else args.data_dir
    setup_logging(data_dir)
    log = logging.getLogger("mailsift")

    from .app import create_app
    from .launcher import clear_instance, launcher_secret, pick_port, record_instance, running_instance_key

    secret = launcher_secret(data_dir)

    def open_browser(port, key):
        url = f"http://127.0.0.1:{port}/?k={key}"
        if args.no_browser and HAS_CONSOLE:
            print(f"Open this link (it works once, for 2 minutes): {url}")
        else:  # without a console, a printed key would land in the log file
            webbrowser.open(url)

    existing = running_instance_key(data_dir, secret)
    if existing:
        log.info("Already running on port %s: reopening the browser", existing[0])
        open_browser(*existing)
        return

    try:
        port = pick_port(args.port)
    except OSError:
        fatal("MailSift couldn't find a free local port to run on.")
        return

    def shutdown():
        log.info("Shutting down")
        clear_instance(data_dir)
        os._exit(0)

    app = create_app(data_dir, port=port, demo=args.demo, dry_run=args.dry_run,
                     launcher_secret=secret, shutdown=shutdown)
    state = app.config["MAILSIFT_STATE"]
    record_instance(data_dir, port)

    if not args.stay:
        def watchdog():
            while True:
                time.sleep(15)
                if state.idle_seconds() > IDLE_EXIT_SECONDS and not state.busy():
                    log.info("Browser tab closed for %d min: exiting", IDLE_EXIT_SECONDS // 60)
                    shutdown()
        threading.Thread(target=watchdog, daemon=True, name="watchdog").start()

    mode = " [DEMO]" if args.demo else " [DRY RUN]" if args.dry_run else ""
    log.info("MailSift %s%s starting on 127.0.0.1:%s", __version__, mode, port)
    print(f"MailSift {__version__}{mode} running on 127.0.0.1:{port}. Use the Quit button in the page to stop it.")
    threading.Timer(0.8, lambda: open_browser(port, app.config["MAILSIFT_NEW_LAUNCH_KEY"]())).start()
    try:
        app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)
    except OSError as exc:
        fatal(f"MailSift couldn't start its local server: {exc}")
    finally:
        clear_instance(data_dir)


if __name__ == "__main__":
    main()
