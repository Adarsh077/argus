"""On-demand screen recording (Linux / KDE-Wayland only).

Records the primary monitor to an H.264 mp4 with a single mixed AAC audio
track (microphone + desktop-output monitor). Triggered manually from the
system tray or the dashboard via the IPC ``start_recording`` /
``stop_recording`` commands — never on a timer.

Pipeline
--------
Video comes from the same XDG ScreenCast portal + PipeWire path the
screenshot loop uses (``screen_wayland._negotiate``), reusing the persisted
``restore_token`` so recording starts SILENTLY, with no consent dialog.
Audio is captured with GStreamer ``pulsesrc`` (via pipewire-pulse): the
default microphone and the default sink's ``.monitor`` device, blended in an
``audiomixer`` into one AAC track and muxed with the video.

    pipewiresrc → videoconvert → x264enc → h264parse ─┐
    pulsesrc(mic) ─┐                                   ├─ mp4mux → filesink
    pulsesrc(sink.monitor) ─┴ audiomixer → aacenc ─────┘

All-or-nothing
--------------
Per the agreed design, a recording only starts if video AND both audio
sources are available. If the mic or the desktop-monitor device can't be
resolved, or the pipeline fails to reach PLAYING, ``start()`` raises and no
file is produced — the caller surfaces the error (tray notification).

Threading / D-Bus lifetime
--------------------------
The portal ties the ScreenCast session to the D-Bus connection's unique
name (see screen_wayland._negotiate) — disconnecting tears the PipeWire node
down. So a dedicated thread runs a private asyncio loop that holds the bus
connected for the whole recording, awaiting a stop event. GStreamer runs on
its own internal threads once PLAYING; the asyncio loop only keeps the bus
(and therefore the screencast node) alive.

Finalizing
----------
mp4 needs a clean EOS so mp4mux writes the moov atom (otherwise the file is
unplayable). ``stop()`` — also called by ``daemon.stop()`` on tray-Quit /
SIGTERM — sends EOS, waits for it, sets the pipeline to NULL, then inserts
the DB row pointing at the finalized file.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from argus.capture.platform.detect import BACKEND_KDE_WAYLAND, detect_platform
from argus.state import State

logger = logging.getLogger("argus.capture.recorder")

# Reuse the screenshot loop's restore_token so recording is silent.
_RESTORE_TOKEN_KEY = "screencast_restore_token"

_STATE_TIMEOUT_S = 10.0
_START_TIMEOUT_S = 20.0
_EOS_TIMEOUT_S = 10.0

# AAC encoder candidates, in preference order (availability varies by the
# installed GStreamer plugin set).
_AAC_ENCODERS = ("voaacenc", "avenc_aac", "fdkaacenc", "faac")


class RecorderError(RuntimeError):
    """Raised by start()/stop() on any failure. Message is user-facing."""


def _default_audio_devices() -> tuple[str, str]:
    """Resolve (mic_device, desktop_monitor_device) via PulseAudio/pipewire.

    Uses ``pactl`` to find the default source (mic) and the default sink,
    whose ``.monitor`` is desktop output. Raises RecorderError if either
    can't be resolved (part of the all-or-nothing contract).
    """
    def _pactl(arg: str) -> str:
        try:
            out = subprocess.run(
                ["pactl", arg],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except FileNotFoundError as exc:
            raise RecorderError("pactl not found; cannot resolve audio devices") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise RecorderError(f"pactl {arg} failed: {exc}") from exc
        name = out.stdout.strip()
        if out.returncode != 0 or not name:
            raise RecorderError(f"pactl {arg} returned no device")
        return name

    mic = _pactl("get-default-source")
    sink = _pactl("get-default-sink")
    return mic, f"{sink}.monitor"


def _pick_aac_encoder() -> str:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    if not Gst.is_initialized():
        Gst.init(None)
    for cand in _AAC_ENCODERS:
        if Gst.ElementFactory.find(cand) is not None:
            return cand
    raise RecorderError(
        "no AAC encoder available (tried " + ", ".join(_AAC_ENCODERS) + ")"
    )


class Recorder:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self._state = State(config.data_dir)
        self._backend = detect_platform().backend

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop = None  # asyncio loop owned by the session thread
        self._stop_event = None  # asyncio.Event, created on the session loop
        self._started = threading.Event()  # set once start succeeds or fails
        self._start_error: Exception | None = None

        self._pipeline = None
        self._active = False
        self._ts_start: datetime | None = None
        self._out_path: Path | None = None
        self._monitors: str | None = None

    # -- public API -----------------------------------------------------
    def is_recording(self) -> bool:
        return self._active

    def start(self) -> dict:
        """Start recording. Blocks until the pipeline is confirmed PLAYING.
        Raises RecorderError on any failure (all-or-nothing)."""
        with self._lock:
            if self._active:
                raise RecorderError("a recording is already in progress")
            if self._backend != BACKEND_KDE_WAYLAND:
                raise RecorderError(
                    f"screen recording is only supported on KDE-Wayland (backend={self._backend!r})"
                )

            self._started.clear()
            self._start_error = None
            self._thread = threading.Thread(
                target=self._run_session, name="argus-recorder", daemon=True
            )
            self._thread.start()

        if not self._started.wait(timeout=_START_TIMEOUT_S):
            raise RecorderError("recording failed to start within timeout")
        if self._start_error is not None:
            err = self._start_error
            raise RecorderError(str(err))
        return {
            "recording": True,
            "path": str(self._out_path),
            "monitors": self._monitors,
        }

    def stop(self) -> dict:
        """Stop the active recording, finalize the mp4, and persist a DB row.
        Safe to call when idle (returns recording=False)."""
        with self._lock:
            if not self._active or self._loop is None:
                return {"recording": False}
            loop = self._loop
            stop_event = self._stop_event
            thread = self._thread
            ts_start = self._ts_start
            out_path = self._out_path
            monitors = self._monitors

        # Signal the session thread to finalize + tear down, then wait.
        loop.call_soon_threadsafe(stop_event.set)
        if thread is not None:
            thread.join(timeout=_EOS_TIMEOUT_S + 10.0)

        ts_end = datetime.now(timezone.utc)
        duration = (ts_end - ts_start).total_seconds() if ts_start else 0.0

        row_id = None
        if out_path is not None and out_path.exists():
            row_id = self.db.insert_recording(
                path=str(out_path),
                monitors=monitors or "unknown",
                ts_start=ts_start.isoformat() if ts_start else ts_end.isoformat(),
                ts_end=ts_end.isoformat(),
                duration_seconds=duration,
            )
            logger.info("Recording saved: %s (%.1fs)", out_path, duration)
        else:
            logger.warning("Recording stopped but no output file at %s", out_path)

        self._active = False
        return {
            "recording": False,
            "path": str(out_path) if out_path else None,
            "duration_seconds": duration,
            "id": row_id,
        }

    # -- session thread -------------------------------------------------
    def _run_session(self) -> None:
        import asyncio

        try:
            asyncio.run(self._session_async())
        except Exception:  # noqa: BLE001 - already recorded in _start_error
            logger.exception("recorder session crashed")
            if not self._started.is_set():
                self._start_error = self._start_error or RecorderError("recorder session crashed")
                self._started.set()

    async def _session_async(self) -> None:
        import asyncio

        from argus.capture.platform.screen_wayland import _negotiate

        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        bus = None
        try:
            # 1) Resolve audio devices FIRST (cheap, and part of the
            #    all-or-nothing precondition) before opening the portal.
            mic_dev, monitor_dev = _default_audio_devices()
            aacenc = _pick_aac_encoder()

            # 2) Portal negotiation (silent via the shared restore_token).
            token = self._state.get(_RESTORE_TOKEN_KEY)
            bus, fd, streams, new_token = await _negotiate(token)
            if new_token and new_token != token:
                self._state.set(_RESTORE_TOKEN_KEY, new_token)
            if not streams:
                raise RecorderError("portal returned no screencast streams")

            node_id, props = streams[0]  # primary = first monitor
            size = props.get("size")
            if size:
                self._monitors = f"node{node_id}:{int(size[0])}x{int(size[1])}"
            else:
                self._monitors = f"node{node_id}"

            # 3) Build the output path.
            ts = datetime.now(timezone.utc)
            out_path = self._build_out_path(ts)

            # 4) Build + start the GStreamer pipeline (blocking → executor).
            noise_suppress = bool(
                self.config.get("capture", "recording_noise_suppression", default=True)
            )
            pipeline = await self._loop.run_in_executor(
                None,
                self._build_and_start_pipeline,
                fd,
                node_id,
                mic_dev,
                monitor_dev,
                aacenc,
                out_path,
                noise_suppress,
            )

            self._pipeline = pipeline
            self._ts_start = ts
            self._out_path = out_path
            self._active = True
            logger.info("Recording started: %s (%s)", out_path, self._monitors)
            self._started.set()
        except Exception as exc:  # noqa: BLE001
            self._start_error = exc
            logger.exception("failed to start recording")
            self._active = False
            if bus is not None:
                try:
                    bus.disconnect()
                except Exception:
                    pass
            self._started.set()
            return

        # 5) Keep the D-Bus connection (and thus the screencast node) alive
        #    until stop is requested.
        try:
            await self._stop_event.wait()
        finally:
            self._finalize_pipeline()
            try:
                bus.disconnect()
            except Exception:
                pass

    # -- GStreamer ------------------------------------------------------
    def _build_and_start_pipeline(
        self,
        fd: int,
        node_id: int,
        mic_dev: str,
        monitor_dev: str,
        aacenc: str,
        out_path: Path,
        noise_suppress: bool,
    ):
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        if not Gst.is_initialized():
            Gst.init(None)

        # Optional mic noise suppression. Best-effort: pick the best filter
        # available and, if none is installed, record the raw mic rather
        # than failing the whole recording.
        #
        # Priority:
        #   1. audiornnoise (gst-plugin-rsaudiofx) — the RNNoise recurrent
        #      neural-net denoiser, the same model OBS's "RNNoise" filter
        #      uses. Best quality.
        #   2. webrtcdsp (gst-plugins-bad / webrtc-audio-processing) —
        #      WebRTC spectral suppression. Lighter, less aggressive.
        # A trailing "audioconvert ! audioresample !" re-normalizes the
        # format the filter emits before it hits the mixer.
        mic_filter = ""
        if noise_suppress:
            if Gst.ElementFactory.find("audiornnoise") is not None:
                mic_filter = "audiornnoise ! audioconvert ! audioresample ! "
            elif Gst.ElementFactory.find("webrtcdsp") is not None:
                mic_filter = (
                    "webrtcdsp echo-cancel=false noise-suppression=true "
                    "noise-suppression-level=high gain-control=true voice-detection=false ! "
                    "audioconvert ! audioresample ! "
                )
            else:
                logger.warning(
                    "noise suppression requested but neither 'audiornnoise' "
                    "(gst-plugin-rsaudiofx) nor 'webrtcdsp' (gst-plugins-bad) is "
                    "installed; recording raw mic"
                )
            if mic_filter:
                logger.info("mic noise suppression: %s", mic_filter.split()[0])

        # `videorate` + a fixed output framerate is REQUIRED: raw frames from
        # the portal's pipewiresrc arrive without reliable PTS, and mp4mux
        # refuses them ("Buffer has no PTS" → "Could not multiplex stream" →
        # a headerless, unplayable file). videorate regenerates monotonic
        # timestamps at a constant rate so the muxer accepts them.
        desc = (
            f"pipewiresrc name=vsrc fd={fd} path={node_id} do-timestamp=true keepalive-time=1000 ! "
            f"videoconvert ! videorate ! video/x-raw,framerate=30/1 ! queue ! "
            f"x264enc tune=zerolatency speed-preset=veryfast key-int-max=60 ! h264parse ! "
            f"queue ! mp4mux name=mux ! filesink name=fsink "
            f"pulsesrc name=amic do-timestamp=true ! audioconvert ! audioresample ! {mic_filter}queue ! mix. "
            f"pulsesrc name=asink do-timestamp=true ! audioconvert ! audioresample ! queue ! mix. "
            f"audiomixer name=mix ! audioconvert ! audioresample ! {aacenc} ! aacparse ! queue ! mux."
        )
        pipeline = Gst.parse_launch(desc)

        # Set path/device via properties (avoids parse_launch quoting issues
        # for paths/device names with spaces or special characters).
        pipeline.get_by_name("fsink").set_property("location", str(out_path))
        pipeline.get_by_name("amic").set_property("device", mic_dev)
        pipeline.get_by_name("asink").set_property("device", monitor_dev)

        pipeline.set_state(Gst.State.PLAYING)
        ret, _cur, _pending = pipeline.get_state(int(_STATE_TIMEOUT_S * Gst.SECOND))
        if ret == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise RecorderError("recording pipeline failed to reach PLAYING")
        return pipeline

    def _finalize_pipeline(self) -> None:
        pipeline = self._pipeline
        if pipeline is None:
            return
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            # Clean EOS so mp4mux writes the moov atom → playable file.
            # EOS must be injected at the SOURCE elements so it flows
            # downstream through the encoders and mux; sending it to the
            # pipeline/bin travels upstream to sinks and never reaches the
            # muxer, leaving a headerless (moov-less) unplayable file.
            for name in ("vsrc", "amic", "asink"):
                el = pipeline.get_by_name(name)
                if el is not None:
                    el.send_event(Gst.Event.new_eos())
            bus = pipeline.get_bus()
            bus.timed_pop_filtered(
                int(_EOS_TIMEOUT_S * Gst.SECOND),
                Gst.MessageType.EOS | Gst.MessageType.ERROR,
            )
            pipeline.set_state(Gst.State.NULL)
        except Exception:
            logger.exception("error finalizing recording pipeline")
        finally:
            self._pipeline = None

    # -- storage --------------------------------------------------------
    def _build_out_path(self, ts: datetime) -> Path:
        day_dir = self.config.recordings_dir / ts.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        filename = ts.strftime("%Y%m%dT%H%M%S%fZ") + ".mp4"
        return day_dir / filename
