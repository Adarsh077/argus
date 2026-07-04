"""Windows screen-capture backend, via `mss`.

UNTESTED: written on a Linux dev machine with no Windows box available.
`mss` works cross-platform, but this code path is only exercised when
detect_platform() reports the "windows" backend, so it is never imported
on Linux.

mss exposes `sct.monitors`: index 0 is the full virtual screen (all
monitors already composited into one bounding box); indices 1..N are the
individual monitors. We grab monitor[0] to get every display in one shot,
already laid out at its true virtual-desktop position, and record the
per-monitor geometry for the DB row.
"""

from __future__ import annotations

import logging

from PIL import Image

logger = logging.getLogger("argus.capture.screen_windows")


def capture_screens() -> tuple[Image.Image | None, str | None]:
    """Grab all monitors composited into one PIL RGB image.

    Returns (image, monitors_info) or (None, None) on failure. Never raises.
    """
    try:
        import mss
    except ImportError:
        logger.error("mss not available; cannot capture screen on Windows. Run `uv add mss`.")
        return None, None

    try:
        with mss.mss() as sct:
            monitors = sct.monitors
            if not monitors:
                logger.warning("mss reported no monitors")
                return None, None

            virtual = monitors[0]  # full bounding box across all displays
            shot = sct.grab(virtual)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

            # Record per-monitor geometry (skip index 0, the composite box).
            parts = []
            for i, m in enumerate(monitors[1:], start=1):
                parts.append(
                    f"mon{i}:{m['width']}x{m['height']}+{m['left']}+{m['top']}"
                )
            info = ";".join(parts) if parts else (
                f"virtual:{virtual['width']}x{virtual['height']}"
            )
            return img, info
    except Exception:
        logger.exception("mss screen capture failed")
        return None, None
