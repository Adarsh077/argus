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
import threading

from argus.config import load_config
from argus.daemon import Daemon
from argus.tray import run_tray

logger = logging.getLogger("argus.windows_main")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    config = load_config()
    daemon = Daemon(config)

    daemon_thread = threading.Thread(
        target=daemon.run_forever, name="argus-daemon-main", daemon=True
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
