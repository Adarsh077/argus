"""Active-window capture loop (5s default interval).

Phase 2: real active-window queries. Backend is chosen from
detect_platform() and the platform-specific modules are imported lazily so
Linux never imports pywin32 and Windows never shells out to kdotool.
"""

from __future__ import annotations

import logging

from argus.capture.base import Capturer
from argus.capture.platform.detect import (
    BACKEND_KDE_WAYLAND,
    BACKEND_WINDOWS,
    detect_platform,
)

logger = logging.getLogger("argus.capture.window")

_UNSUPPORTED_LOGGED = False


class WindowCapturer(Capturer):
    name = "window"

    def __init__(self, config, db):
        super().__init__(config, db)
        self._backend = detect_platform().backend

    def capture(self) -> None:
        if self._backend == BACKEND_KDE_WAYLAND:
            from argus.capture.platform.kde_wayland import get_active_window
        elif self._backend == BACKEND_WINDOWS:
            from argus.capture.platform.windows import get_active_window
        else:
            global _UNSUPPORTED_LOGGED
            if not _UNSUPPORTED_LOGGED:
                logger.warning(
                    "No window-capture backend for platform %r; window_events "
                    "will not be populated.",
                    self._backend,
                )
                _UNSUPPORTED_LOGGED = True
            return

        info = get_active_window()
        if info is None:
            # No active window (desktop focused) or a transient backend
            # failure. Skip this tick rather than writing a fake row.
            return

        self.db.insert_window_event(
            app=info.app,
            window_title=info.window_title or "",
            monitor=info.monitor or "unknown",
        )
