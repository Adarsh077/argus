"""KDE-Wayland silent screen capture via XDG Desktop Portal + PipeWire.

Wayland forbids clients from reading the screen directly. The supported
path is the `org.freedesktop.portal.ScreenCast` D-Bus portal, which hands
back PipeWire stream node ids that we pull single frames from with
GStreamer's `pipewiresrc`.

Silent re-capture (the spec's hard requirement: NO per-capture prompt)
works via a `restore_token`:

  1. First run: CreateSession -> SelectSources(persist_mode=2, multiple,
     types=MONITOR) -> Start. KDE shows ONE consent dialog. The Start
     response returns a `restore_token`, which we persist (state.json).
  2. Later runs: pass that `restore_token` back into SelectSources. KDE
     restores the previous selection WITHOUT prompting -> silent capture.

Portal D-Bus is async (Request/Response objects), so the whole negotiation
runs in a private asyncio loop inside `capture_screens()` (called from the
daemon's screen thread). GStreamer frame pull is synchronous.

Everything is defensive: portal missing, consent denied, no restore_token
support, or a transient PipeWire hiccup return (None, None, token) so the
daemon loop logs and moves on rather than crashing.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass

from PIL import Image

logger = logging.getLogger("argus.capture.screen_wayland")

# org.freedesktop.portal.ScreenCast source types (bitmask)
_TYPE_MONITOR = 1
_TYPE_WINDOW = 2

# CursorMode: 1 = hidden, 2 = embedded, 4 = metadata
_CURSOR_HIDDEN = 1

# PersistMode: 0 = do not persist, 1 = until app closes, 2 = until revoked
_PERSIST_UNTIL_REVOKED = 2

_PORTAL_BUS = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_SCREENCAST_IFACE = "org.freedesktop.portal.ScreenCast"
_REQUEST_IFACE = "org.freedesktop.portal.Request"

_FRAME_TIMEOUT_S = 8.0

# Hand-written introspection XML. We can't use bus.introspect() on the portal
# object: dbus-next's strict validator rejects a hyphenated property name
# ("power-saver-enabled") exposed by an unrelated interface on the same object.
# We only need ScreenCast + Request, so we declare them ourselves.
_SCREENCAST_XML = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.freedesktop.portal.ScreenCast">
    <method name="CreateSession">
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="SelectSources">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="Start">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="s" name="parent_window" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="OpenPipeWireRemote">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="h" name="fd" direction="out"/>
    </method>
  </interface>
</node>"""

_REQUEST_XML = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN" "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.freedesktop.portal.Request">
    <method name="Close"/>
    <signal name="Response">
      <arg type="u" name="response"/>
      <arg type="a{sv}" name="results"/>
    </signal>
  </interface>
</node>"""


@dataclass
class MonitorFrame:
    image: Image.Image
    x: int
    y: int
    w: int
    h: int


def _token() -> str:
    return "argus_" + secrets.token_hex(8)


async def _await_request(bus, method_reply_path: str):
    """Given the object path returned by a portal method, wait for the
    Request's Response signal and return (response_code, results_dict).

    Values in results are unwrapped from dbus_next Variants.
    """
    from dbus_next.introspection import Node

    future: asyncio.Future = asyncio.get_running_loop().create_future()

    # Build a proxy for the request object using our own Request XML.
    obj = bus.get_proxy_object(_PORTAL_BUS, method_reply_path, Node.parse(_REQUEST_XML))
    request_iface = obj.get_interface(_REQUEST_IFACE)

    def on_response(response: int, results: dict):
        if not future.done():
            unwrapped = {k: (v.value if hasattr(v, "value") else v) for k, v in results.items()}
            future.set_result((response, unwrapped))

    request_iface.on_response(on_response)
    try:
        return await asyncio.wait_for(future, timeout=120.0)
    finally:
        try:
            request_iface.off_response(on_response)
        except Exception:
            pass


def _v(signature: str, value):
    from dbus_next import Variant

    return Variant(signature, value)


async def _negotiate(restore_token: str | None):
    """Run the full portal negotiation.

    Returns (fd, streams, new_restore_token) where streams is a list of
    (node_id, props_dict). Raises on hard failure (caller catches).
    """
    from dbus_next.aio import MessageBus
    from dbus_next.constants import BusType
    from dbus_next.introspection import Node

    bus = await MessageBus(bus_type=BusType.SESSION, negotiate_unix_fd=True).connect()
    try:
        portal = bus.get_proxy_object(_PORTAL_BUS, _PORTAL_PATH, Node.parse(_SCREENCAST_XML))
        sc = portal.get_interface(_SCREENCAST_IFACE)

        # 1) CreateSession
        reply_path = await sc.call_create_session({
            "handle_token": _v("s", _token()),
            "session_handle_token": _v("s", _token()),
        })
        code, results = await _await_request(bus, reply_path)
        if code != 0:
            raise RuntimeError(f"CreateSession failed/cancelled (code={code})")
        session_handle = results["session_handle"]
        logger.info("Portal session created: %s", session_handle)

        # 2) SelectSources (this is where restore_token makes it silent)
        select_opts = {
            "handle_token": _v("s", _token()),
            "types": _v("u", _TYPE_MONITOR),
            "multiple": _v("b", True),
            "cursor_mode": _v("u", _CURSOR_HIDDEN),
            "persist_mode": _v("u", _PERSIST_UNTIL_REVOKED),
        }
        if restore_token:
            select_opts["restore_token"] = _v("s", restore_token)
            logger.info("Reusing restore_token for silent capture")
        else:
            logger.info("No restore_token yet: first run WILL show the KDE consent dialog")

        reply_path = await sc.call_select_sources(session_handle, select_opts)
        code, results = await _await_request(bus, reply_path)
        if code != 0:
            raise RuntimeError(f"SelectSources failed/cancelled (code={code})")

        # 3) Start (shows dialog on first run; silent when restored)
        reply_path = await sc.call_start(session_handle, "", {"handle_token": _v("s", _token())})
        code, results = await _await_request(bus, reply_path)
        if code != 0:
            raise RuntimeError(f"Start denied/cancelled by user (code={code})")

        streams = results.get("streams", [])
        # streams entries are [node_id, props]; props Variants already unwrapped
        parsed_streams = []
        for entry in streams:
            node_id = entry[0]
            props = entry[1]
            props = {k: (v.value if hasattr(v, "value") else v) for k, v in props.items()}
            parsed_streams.append((node_id, props))

        new_token = results.get("restore_token")
        if new_token:
            logger.info("Received restore_token from portal (will persist)")
        else:
            logger.warning(
                "Portal returned no restore_token; silent re-capture unsupported "
                "on this setup -- consent dialog may appear each run."
            )

        if not parsed_streams:
            raise RuntimeError("Start returned no streams")

        # 4) OpenPipeWireRemote -> fd
        fd = await sc.call_open_pipe_wire_remote(session_handle, {})
        logger.info("PipeWire remote fd=%s, %d stream(s)", fd, len(parsed_streams))
        return bus, fd, parsed_streams, new_token
    except Exception:
        # Only disconnect on failure. On success the bus MUST stay connected
        # through the frame grab (see _orchestrate): the portal ties the
        # ScreenCast session to this bus's unique name, so disconnecting early
        # tears down the screencast node -- pipewiresrc then falls back to a
        # default PipeWire source (e.g. the webcam) and grabs the wrong stream.
        bus.disconnect()
        raise


async def _orchestrate(restore_token: str | None):
    """Negotiate, grab every stream's frame while the portal session (and
    therefore its PipeWire nodes) is still alive, then tear down.

    Returns (frames_data, monitors_info, new_token) where frames_data is a
    list of (PIL.Image, x, y, w, h).
    """
    bus, fd, streams, new_token = await _negotiate(restore_token)
    loop = asyncio.get_running_loop()
    frames_data = []
    info_parts: list[str] = []
    fallback_x = 0
    try:
        for node_id, props in streams:
            img = None
            try:
                # Grab in a worker thread: the blocking GStreamer pull must not
                # stall the asyncio loop that keeps the D-Bus session alive.
                img = await loop.run_in_executor(None, _grab_frame, fd, node_id)
            except Exception:
                logger.exception("Frame grab failed for node %s", node_id)
            if img is None:
                continue

            pos = props.get("position")
            size = props.get("size")
            if pos and size:
                x, y = int(pos[0]), int(pos[1])
                w, h = int(size[0]), int(size[1])
            else:
                x, y = fallback_x, 0
                w, h = img.width, img.height
                fallback_x += img.width
            frames_data.append((img, x, y, w, h))
            info_parts.append(f"node{node_id}:{img.width}x{img.height}+{x}+{y}")
    finally:
        bus.disconnect()

    return frames_data, (";".join(info_parts) if info_parts else None), new_token


def _grab_frame(fd: int, node_id: int) -> Image.Image | None:
    """Pull a single frame from one PipeWire stream node via GStreamer."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    if not Gst.is_initialized():
        Gst.init(None)

    pipeline = Gst.parse_launch(
        f"pipewiresrc fd={fd} path={node_id} always-copy=true do-timestamp=true ! "
        f"videoconvert ! video/x-raw,format=RGB ! "
        f"appsink name=sink max-buffers=1 drop=true sync=false"
    )
    sink = pipeline.get_by_name("sink")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        # The state change to PLAYING is asynchronous for a live source. If we
        # try to pull before the pipeline has actually rolled we can time out
        # even though the stream is fine (this is the cold-start race that
        # made the very first post-consent grab fail). Wait for the transition
        # to settle first.
        ret, _cur, _pending = pipeline.get_state(int(_FRAME_TIMEOUT_S * Gst.SECOND))
        if ret == Gst.StateChangeReturn.FAILURE:
            logger.warning("Pipeline failed to reach PLAYING for node %s", node_id)
            return None

        # Pull the first frame; retry once, since PipeWire may need a beat to
        # deliver the opening buffer after negotiation.
        sample = None
        for _ in range(2):
            sample = sink.emit("try-pull-sample", int(_FRAME_TIMEOUT_S * Gst.SECOND))
            if sample is not None:
                break
        if sample is None:
            logger.warning("No frame from PipeWire node %s within %ss", node_id, _FRAME_TIMEOUT_S)
            return None

        caps = sample.get_caps().get_structure(0)
        width = caps.get_value("width")
        height = caps.get_value("height")
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            logger.warning("Could not map GStreamer buffer for node %s", node_id)
            return None
        try:
            data = bytes(mapinfo.data)
            # GStreamer RGB rows are padded to a 4-byte stride.
            stride = (width * 3 + 3) & ~3
            expected_tight = width * height * 3
            if len(data) >= stride * height and stride != width * 3:
                img = Image.frombytes("RGB", (width, height), data, "raw", "RGB", stride)
            elif len(data) >= expected_tight:
                img = Image.frombytes("RGB", (width, height), data[:expected_tight], "raw", "RGB")
            else:
                logger.warning(
                    "Unexpected buffer size for node %s: %d bytes for %dx%d",
                    node_id, len(data), width, height,
                )
                return None
            return img.copy()
        finally:
            buf.unmap(mapinfo)
    finally:
        pipeline.set_state(Gst.State.NULL)


def capture_screens(restore_token: str | None):
    """Capture every monitor and return (frames, monitors_info, new_token).

    frames: list[MonitorFrame] (may be empty on failure)
    monitors_info: str for the DB row (or None)
    new_token: restore_token to persist (or None if unchanged/unsupported)

    Never raises: on any failure returns ([], None, restore_token).
    """
    try:
        frames_data, info, new_token = asyncio.run(_orchestrate(restore_token))
    except Exception:
        logger.exception("Portal ScreenCast capture failed")
        return [], None, restore_token

    frames = [MonitorFrame(image=img, x=x, y=y, w=w, h=h) for (img, x, y, w, h) in frames_data]

    # Persist the (possibly refreshed) token even on partial frame failure.
    return frames, info, (new_token or restore_token)
