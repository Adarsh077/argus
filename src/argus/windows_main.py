"""Windows combined entrypoint: daemon (with in-process dashboard) + tray.

The PyInstaller `.exe` built from `packaging/windows/argus.spec` points at
`argus.windows_main:main` (see that spec's Analysis entry point). Neither
`argus run` nor `argus tray` alone is right for the shipped Windows
installer: the spec calls for a single double-clickable app that starts
capture *and* shows the tray icon together, with no separate console
window and no separate process the user has to launch by hand.

This module starts the `Daemon` (which already serves the dashboard
in-process, see `argus.daemon.Daemon._dashboard_loop`) on a background
thread, then runs the tray icon's blocking main loop on the main thread
(required on Windows/pystray — the tray/menu event loop must own the
main thread). Ctrl-C or "Quit Argus" in the tray menu stops both.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import threading

from argus.config import default_data_dir, load_config
from argus.daemon import Daemon
from argus.tray import run_tray

logger = logging.getLogger("argus.windows_main")


def _setup_logging() -> Path:
    """Log to a rotating file under the data dir.

    The shipped Windows build runs with no console (``console=False`` in
    argus.spec, launched via Task Scheduler / pythonw), so stderr goes
    nowhere. Without a file sink every log line — including the reason a
    subsystem like the dashboard failed to start — is silently lost, which
    makes field debugging impossible. Route all logging to a file so there
    is always something to read.
    """
    log_dir = default_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "argus.log"

    handler = RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return log_path


def main() -> int:
    log_path = _setup_logging()
    logger.info("Argus (Windows) starting; logging to %s", log_path)

    config = load_config()
    daemon = Daemon(config)

    def _run_daemon() -> None:
        # Any exception raised while starting/running the daemon would
        # otherwise die on this background thread and print to a
        # non-existent stderr; log it so the file sink captures the cause.
        try:
            daemon.run_forever()
        except Exception:
            logger.exception("daemon thread crashed")

    daemon_thread = threading.Thread(
        target=_run_daemon, name="argus-daemon-main", daemon=True
    )
    daemon_thread.start()

    try:
        # Tray owns the main thread's event loop (pystray requirement on
        # Windows/macOS backends); it talks to the daemon only via IPC,
        # exactly like the standalone `argus tray` command does.
        run_tray(config)
    finally:
        daemon.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
