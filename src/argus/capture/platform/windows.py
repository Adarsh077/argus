"""Windows active-window backend, via pywin32 + psutil.

UNTESTED: written on a Linux dev machine with no Windows box available.
pywin32 is only installed on Windows (see pyproject.toml environment
marker), and this module is only ever imported when detect_platform()
reports the "windows" backend, so importing it on Linux never happens and
never needs to succeed.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("argus.capture.windows")


class WindowInfo:
    __slots__ = ("app", "window_title", "monitor")

    def __init__(self, app: str, window_title: str | None, monitor: str | None):
        self.app = app
        self.window_title = window_title
        self.monitor = monitor


def get_active_window() -> WindowInfo | None:
    """Return metadata for the currently focused window, or None on any
    failure (no foreground window, permissions, etc). Never raises."""
    try:
        import psutil
        import win32api
        import win32con
        import win32gui
        import win32process
    except ImportError:
        logger.error(
            "pywin32/psutil not available. Install them (pywin32 is an "
            "optional, Windows-only dependency) to enable window capture."
        )
        return None

    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None

        window_title = win32gui.GetWindowText(hwnd) or None

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        app = "unknown"
        if pid:
            try:
                app = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                app = "unknown"

        monitor = None
        try:
            hmonitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
            monitor_info = win32api.GetMonitorInfo(hmonitor)
            monitor = monitor_info.get("Device")
        except Exception:
            logger.debug("Could not resolve monitor for foreground window", exc_info=True)

        return WindowInfo(app=app, window_title=window_title, monitor=monitor)
    except Exception:
        logger.exception("Failed to read active window (Windows backend)")
        return None
