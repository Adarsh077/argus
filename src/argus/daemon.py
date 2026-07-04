"""Argus daemon: orchestrates the capture loops.

Phase 1: three loops (window/screen/camera) run concurrently on their own
intervals, each calling a (stubbed) Capturer that writes one DB row per
iteration. A `paused` flag exists as a placeholder for the future pause
control channel (tray icon, dashboard) — loops just skip capture while set.
"""

from __future__ import annotations

import logging
import threading
import time

from argus.capture.camera import CameraCapturer
from argus.capture.platform.detect import detect_platform
from argus.capture.screen import ScreenCapturer
from argus.capture.window import WindowCapturer
from argus.config import Config
from argus.db import Database
from argus.ipc import IPCServer, default_socket_path
from argus.retention import purge as run_purge

logger = logging.getLogger("argus.daemon")

_RETENTION_INTERVAL_SECONDS = 24 * 60 * 60  # once a day is plenty; purge is cheap but not free


class Daemon:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.db_path)
        self.platform_info = detect_platform()

        self.paused = threading.Event()  # set() == paused; clear() == running
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._start_time: float | None = None
        self._dashboard_server = None  # uvicorn.Server, set if served in-process

        self.window_capturer = WindowCapturer(config, self.db)
        self.screen_capturer = ScreenCapturer(config, self.db)
        self.camera_capturer = CameraCapturer(config, self.db)

        # IPC control channel (pause/resume/status/quit). In-memory only —
        # `paused` resets to False (running) on every daemon restart; there
        # is no persistence of pause state across process lifetimes.
        self._ipc = IPCServer(
            handlers={
                "ping": lambda: {"pong": True},
                "status": self._ipc_status,
                "pause": self._ipc_pause,
                "resume": self._ipc_resume,
                "quit": self._ipc_quit,
            },
            socket_path=default_socket_path(config.data_dir),
        )

    # -- pause control -----------------------------------------------
    def pause(self) -> None:
        self.paused.set()

    def resume(self) -> None:
        self.paused.clear()

    def is_paused(self) -> bool:
        return self.paused.is_set()

    # -- IPC handlers -------------------------------------------------
    def _ipc_status(self) -> dict:
        uptime = time.time() - self._start_time if self._start_time else 0.0
        return {
            "running": True,
            "paused": self.is_paused(),
            "uptime_seconds": uptime,
            "row_counts": self.db.row_counts(),
        }

    def _ipc_pause(self) -> dict:
        self.pause()
        logger.info("Paused via IPC")
        return {"paused": True}

    def _ipc_resume(self) -> dict:
        self.resume()
        logger.info("Resumed via IPC")
        return {"paused": False}

    def _ipc_quit(self) -> dict:
        logger.info("Quit requested via IPC")
        threading.Thread(target=self._stop.set, daemon=True).start()
        return {"quitting": True}

    # -- loop machinery -------------------------------------------------
    def _loop(self, name: str, interval: float, fn) -> None:
        logger.info("%s loop starting (interval=%ss)", name, interval)
        while not self._stop.is_set():
            if not self.paused.is_set():
                try:
                    fn()
                except Exception:
                    logger.exception("%s capture failed", name)
            else:
                logger.debug("%s loop skipped (paused)", name)
            self._stop.wait(interval)
        logger.info("%s loop stopped", name)

    def start(self) -> None:
        logger.info(
            "Argus daemon starting: os=%s session=%s desktop=%s backend=%s",
            self.platform_info.os_name,
            self.platform_info.session_type,
            self.platform_info.desktop,
            self.platform_info.backend,
        )

        specs = [
            ("window", self.config.get("capture", "window_interval_seconds", default=5), self.window_capturer.capture),
            ("screen", self.config.get("capture", "screen_interval_seconds", default=300), self.screen_capturer.capture),
            ("camera", self.config.get("capture", "camera_interval_seconds", default=300), self.camera_capturer.capture),
        ]

        for name, interval, fn in specs:
            t = threading.Thread(target=self._loop, args=(name, interval, fn), name=f"argus-{name}", daemon=True)
            self._threads.append(t)
            t.start()

        # Retention purge: data-cleanup housekeeping, NOT report generation
        # (reports remain on-demand only). Runs once immediately on
        # startup, then once every 24h. Cheap (a handful of indexed SQL
        # deletes + filesystem unlinks), so a daily cadence is plenty.
        t = threading.Thread(target=self._retention_loop, name="argus-retention", daemon=True)
        self._threads.append(t)
        t.start()

        self._start_time = time.time()
        self._ipc.start()

        # Serve the local web dashboard in-process so `argus run` (and the
        # installed service) expose the localhost page the tray's "Open
        # dashboard" points at — per spec §81 "the daemon serves a localhost
        # page". Decoupled from capture: it only reads the DB/images and
        # issues IPC control commands. Toggle off via config if a standalone
        # `argus dashboard` process is preferred.
        if self.config.get("dashboard", "serve_with_daemon", default=True):
            t = threading.Thread(target=self._dashboard_loop, name="argus-dashboard", daemon=True)
            self._threads.append(t)
            t.start()

    def _dashboard_loop(self) -> None:
        # Everything here — imports, create_app(), Server construction, and
        # server.run() — runs on this background thread, so it must all be
        # under one try/except: an exception anywhere would otherwise die
        # silently on the thread (no console on the frozen Windows build) and
        # the dashboard would just never come up with nothing logged.
        port = self.config.get("dashboard", "port", default=8477)
        try:
            import uvicorn

            from argus.dashboard.app import create_app

            app = create_app(self.config)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    log_level="warning",
                    # Force the pure-Python asyncio loop + h11 HTTP
                    # implementation instead of uvicorn[standard]'s "auto"
                    # (which prefers the optional C extensions
                    # uvloop/httptools). Those extensions are routinely missed
                    # by PyInstaller's static analysis, and a missing httptools
                    # crashes the server thread at startup. h11 is pure Python,
                    # always bundled, and more than fast enough for a localhost
                    # single-user dashboard. uvloop is Unix-only anyway.
                    loop="asyncio",
                    http="h11",
                    ws="none",
                )
            )
            server.install_signal_handlers = lambda: None  # not on main thread
            self._dashboard_server = server
            logger.info("dashboard serving on http://127.0.0.1:%d", port)
            server.run()
            logger.info("dashboard stopped")
        except Exception:
            logger.exception("dashboard failed to start / crashed")

    def _retention_loop(self) -> None:
        logger.info("retention loop starting (interval=%ss)", _RETENTION_INTERVAL_SECONDS)
        while not self._stop.is_set():
            try:
                run_purge(self.db, self.config)
            except Exception:
                logger.exception("retention purge failed")
            self._stop.wait(_RETENTION_INTERVAL_SECONDS)
        logger.info("retention loop stopped")

    def stop(self) -> None:
        logger.info("Argus daemon stopping...")
        self._stop.set()
        if self._dashboard_server is not None:
            self._dashboard_server.should_exit = True
        self._ipc.stop()
        for t in self._threads:
            t.join(timeout=5)
        self.db.close()
        logger.info("Argus daemon stopped.")

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
