"""KDE-Wayland active-window backend, via the `kdotool` CLI.

KWin on Wayland has no equivalent of xdotool/wmctrl (those are X11-only and
do not see the compositor's real window state under Wayland). `kdotool`
(https://github.com/jinliu/kdotool) drives KWin's scripting API instead and
is the supported way to query the active window on this stack.

This module shells out to `kdotool` per call. That is fine at a 5s poll
interval but keep the command list short — each capture makes 1-3
subprocess calls.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

logger = logging.getLogger("argus.capture.kde_wayland")

_KDOTOOL_MISSING_LOGGED = False


class WindowInfo:
    __slots__ = ("app", "window_title", "monitor")

    def __init__(self, app: str, window_title: str | None, monitor: str | None):
        self.app = app
        self.window_title = window_title
        self.monitor = monitor


def _kdotool_available() -> bool:
    global _KDOTOOL_MISSING_LOGGED
    if shutil.which("kdotool") is not None:
        return True
    if not _KDOTOOL_MISSING_LOGGED:
        logger.error(
            "kdotool not found on PATH. Active-window capture on KDE-Wayland "
            "requires kdotool (https://github.com/jinliu/kdotool) since "
            "xdotool/wmctrl do not work under KWin-Wayland. Install it and "
            "restart Argus; until then window_events rows will be skipped."
        )
        _KDOTOOL_MISSING_LOGGED = True
    return False


def _run(args: list[str], timeout: float = 2.0) -> str | None:
    try:
        result = subprocess.run(
            ["kdotool", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        logger.warning("kdotool %s timed out", " ".join(args))
        return None
    except Exception:
        logger.exception("kdotool %s failed unexpectedly", " ".join(args))
        return None

    if result.returncode != 0:
        # Common, expected case: no window currently has focus (e.g. desktop
        # or an overview is focused). Not an error worth logging loudly.
        logger.debug("kdotool %s exited %s: %s", args, result.returncode, result.stderr.strip())
        return None

    output = result.stdout.strip()
    return output or None


def get_active_window() -> WindowInfo | None:
    """Return metadata for the currently focused window, or None if there
    is no active window or kdotool is unavailable/fails. Never raises."""
    if not _kdotool_available():
        return None

    window_id = _run(["getactivewindow"])
    if window_id is None:
        return None

    window_title = _run(["getwindowname", window_id])
    window_class = _run(["getwindowclassname", window_id])

    app = window_class or "unknown"
    monitor = None  # kdotool does not expose per-window monitor info.

    return WindowInfo(app=app, window_title=window_title, monitor=monitor)
