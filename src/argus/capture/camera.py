"""Camera capture loop (5min default interval).

Phase 4: real camera capture. Every tick we open the configured camera
device, request its native/maximum resolution, read exactly one frame,
and release the device immediately so other apps can use the webcam
between our capture ticks. The frame is converted BGR (cv2) -> RGB -> PIL
and stored as WebP, organised by day so retention can purge whole date
folders:

    <data>/images/camera/YYYY-MM-DD/<utc-ts>Z.webp

Backend selection (cv2.VideoCapture capture API flag):
  - Linux   -> cv2.CAP_V4L2
  - Windows -> cv2.CAP_DSHOW
  - other   -> cv2.CAP_ANY (let OpenCV pick)

Any failure (camera missing/busy/permission-denied/no frame) is logged
clearly and the tick is skipped -- no DB row is written and the loop
never crashes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from PIL import Image

from argus.capture.base import Capturer
from argus.capture.platform.detect import BACKEND_WINDOWS, detect_platform

logger = logging.getLogger("argus.capture.camera")

# Ask the driver for an effectively unbounded resolution so it reports back
# its true native/maximum resolution instead of capping us to a default
# (commonly 640x480) request size.
_MAX_DIM = 10000


class CameraCapturer(Capturer):
    name = "camera"

    def __init__(self, config, db):
        super().__init__(config, db)
        self._backend = detect_platform().backend

    def capture(self) -> None:
        if not self.config.get("capture", "camera_enabled", default=True):
            return

        device_index = self.config.get("capture", "camera_device_index", default=0)
        quality = self.config.get("capture", "image_webp_quality", default=80)

        image = self._grab_frame(device_index)
        if image is None:
            logger.warning("Camera capture produced no frame this tick; skipping")
            return

        ts = datetime.now(timezone.utc)
        path = self._save(image, ts, quality)
        self.db.insert_camera_frame(path=str(path), device=str(device_index), ts=ts.isoformat())
        logger.info("Camera frame saved: %s (%dx%d)", path, image.width, image.height)

    # -- capture ----------------------------------------------------------
    def _grab_frame(self, device_index: int) -> Image.Image | None:
        try:
            import cv2
        except ImportError:
            logger.warning("opencv-python not installed; camera capture unavailable")
            return None

        api_pref = cv2.CAP_DSHOW if self._backend == BACKEND_WINDOWS else cv2.CAP_V4L2

        cap = cv2.VideoCapture(device_index, api_pref)
        try:
            if not cap.isOpened():
                # Fall back to the generic backend in case the preferred one
                # isn't supported for this device.
                cap.release()
                cap = cv2.VideoCapture(device_index, cv2.CAP_ANY)

            if not cap.isOpened():
                logger.warning(
                    "Could not open camera device %s (missing, busy, or permission denied)",
                    device_index,
                )
                return None

            # Request an oversized resolution; drivers clamp this to their
            # actual native/maximum resolution rather than downscaling.
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, _MAX_DIM)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, _MAX_DIM)

            ok, frame = cap.read()
            if not ok or frame is None:
                logger.warning("Camera device %s opened but returned no frame", device_index)
                return None

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            return Image.fromarray(rgb)
        except Exception:
            logger.exception("Unexpected error capturing from camera device %s", device_index)
            return None
        finally:
            cap.release()

    # -- storage ------------------------------------------------------------
    def _save(self, image: Image.Image, ts: datetime, quality: int):
        day_dir = self.config.images_dir / "camera" / ts.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        filename = ts.strftime("%Y%m%dT%H%M%S%f") + "Z.webp"
        path = day_dir / filename
        image.save(path, format="WEBP", quality=quality)
        return path
