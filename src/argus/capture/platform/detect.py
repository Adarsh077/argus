"""Detect OS and, on Linux, session type / desktop, to pick a backend.

Only KDE-Wayland and Windows are in scope for v1. Anything else logs a
clear "unsupported" message and falls back to a generic backend id so the
daemon can still run (with stub capturers) for local development.
"""

from __future__ import annotations

import logging
import os
import platform
from dataclasses import dataclass

logger = logging.getLogger("argus.detect")

BACKEND_KDE_WAYLAND = "kde-wayland"
BACKEND_WINDOWS = "windows"
BACKEND_UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class PlatformInfo:
    os_name: str  # "Linux", "Windows", "Darwin", ...
    session_type: str | None  # e.g. "wayland", "x11" (Linux only)
    desktop: str | None  # e.g. "KDE" (Linux only)
    backend: str  # one of BACKEND_*


def detect_platform() -> PlatformInfo:
    system = platform.system()

    if system == "Windows":
        return PlatformInfo(os_name=system, session_type=None, desktop=None, backend=BACKEND_WINDOWS)

    if system == "Linux":
        session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")

        is_wayland = session_type == "wayland"
        is_kde = "kde" in desktop.lower()

        if is_wayland and is_kde:
            return PlatformInfo(
                os_name=system,
                session_type=session_type,
                desktop=desktop,
                backend=BACKEND_KDE_WAYLAND,
            )

        logger.warning(
            "Unsupported Linux session for Argus v1: session_type=%r desktop=%r "
            "(only KDE-Wayland is supported). Running with stub/no-op backend.",
            session_type,
            desktop,
        )
        return PlatformInfo(
            os_name=system,
            session_type=session_type or None,
            desktop=desktop or None,
            backend=BACKEND_UNSUPPORTED,
        )

    logger.warning(
        "Unsupported OS for Argus v1: %r (only Windows and KDE-Wayland are supported). "
        "Running with stub/no-op backend.",
        system,
    )
    return PlatformInfo(os_name=system, session_type=None, desktop=None, backend=BACKEND_UNSUPPORTED)
