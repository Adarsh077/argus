"""Screenshot capture loop (5min default interval).

Phase 3: real all-monitors capture, composited into one image and stored
as WebP, organised by day so retention can purge whole date folders:

    <data>/images/screenshots/YYYY-MM-DD/<utc-ts>.webp

Backend is chosen from detect_platform():
  - windows      -> mss (screen_windows), monitor[0] = full virtual screen
  - kde-wayland  -> XDG ScreenCast portal + PipeWire (screen_wayland)
  - unsupported  -> logged once, no capture (never crashes the loop)

Compositing lives here so both backends share it: each backend yields
per-monitor frames with their virtual-desktop position; we paint them onto
one canvas sized to the bounding box. Where a backend gives no positions,
frames are laid out side by side.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from PIL import Image

from argus.capture.base import Capturer
from argus.capture.platform.detect import (
    BACKEND_KDE_WAYLAND,
    BACKEND_WINDOWS,
    detect_platform,
)
from argus.state import State

logger = logging.getLogger("argus.capture.screen")

_RESTORE_TOKEN_KEY = "screencast_restore_token"
_UNSUPPORTED_LOGGED = False


class ScreenCapturer(Capturer):
    name = "screen"

    def __init__(self, config, db):
        super().__init__(config, db)
        self._backend = detect_platform().backend
        self._state = State(config.data_dir)

    def capture(self) -> None:
        if not self.config.get("capture", "screen_enabled", default=True):
            return

        if self._backend == BACKEND_WINDOWS:
            image, monitors = self._capture_windows()
        elif self._backend == BACKEND_KDE_WAYLAND:
            image, monitors = self._capture_wayland()
        else:
            global _UNSUPPORTED_LOGGED
            if not _UNSUPPORTED_LOGGED:
                logger.warning(
                    "No screen-capture backend for platform %r; screenshots "
                    "will not be captured.",
                    self._backend,
                )
                _UNSUPPORTED_LOGGED = True
            return

        if image is None:
            logger.warning("Screen capture produced no image this tick; skipping")
            return

        ts = datetime.now(timezone.utc)
        path = self._save(image, ts)
        self.db.insert_screenshot(path=str(path), monitors=monitors or "unknown", ts=ts.isoformat())
        logger.info("Screenshot saved: %s (%s)", path, monitors)

    # -- backends -------------------------------------------------------
    def _capture_windows(self):
        from argus.capture.platform.screen_windows import capture_screens

        return capture_screens()

    def _capture_wayland(self):
        from argus.capture.platform.screen_wayland import capture_screens

        token = self._state.get(_RESTORE_TOKEN_KEY)
        frames, info, new_token = capture_screens(token)

        # Persist the (possibly refreshed) token for silent future captures.
        if new_token and new_token != token:
            self._state.set(_RESTORE_TOKEN_KEY, new_token)

        if not frames:
            return None, None

        image = composite_frames(frames)
        return image, info

    # -- storage --------------------------------------------------------
    def _save(self, image: Image.Image, ts: datetime):
        day_dir = self.config.images_dir / "screenshots" / ts.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        filename = ts.strftime("%Y%m%dT%H%M%S%fZ") + ".webp"
        path = day_dir / filename
        quality = self.config.get("capture", "image_webp_quality", default=80)
        image.save(path, format="WEBP", quality=quality)
        return path


def composite_frames(frames) -> Image.Image:
    """Paint per-monitor frames onto one canvas at their virtual positions.

    `frames` is a list of objects with .image/.x/.y/.w/.h (MonitorFrame).
    The canvas is the bounding box over all frames, normalised so the
    top-left-most monitor sits at (0, 0).
    """
    min_x = min(f.x for f in frames)
    min_y = min(f.y for f in frames)
    max_x = max(f.x + f.image.width for f in frames)
    max_y = max(f.y + f.image.height for f in frames)

    canvas = Image.new("RGB", (max_x - min_x, max_y - min_y), color=(0, 0, 0))
    for f in frames:
        canvas.paste(f.image, (f.x - min_x, f.y - min_y))
    return canvas
