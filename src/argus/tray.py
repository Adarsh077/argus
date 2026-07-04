"""System tray icon: pause/resume/quit/open-dashboard, per spec UI §75-79.

Talks to the daemon exclusively over the IPC control channel in
``argus.ipc`` — the tray has no direct access to capture state, matching
the same decoupled-control pattern the dashboard uses.

Quit semantics
--------------
"Quit Argus" in the tray menu sends the IPC `quit` command (which cleanly
stops the daemon's capture loops and removes the control socket) and then
also exits the tray process itself. It does NOT just close the tray while
leaving the daemon running — the label means "stop Argus entirely".

KDE Plasma / Wayland notes
---------------------------
KDE Plasma has a native StatusNotifierItem/AppIndicator host, so pystray's
``appindicator`` backend (via libayatana-appindicator3 / libappindicator3)
should show up in the system tray without extra Plasma-side configuration.
See run_tray()'s docstring / this module's report for what was verified on
this machine.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
import webbrowser

from PIL import Image, ImageDraw

from argus.config import Config
from argus.ipc import IPCError, default_socket_path, send_command

logger = logging.getLogger("argus.tray")

_POLL_INTERVAL_SECONDS = 3


def _patch_pystray_icon_extension(icon_dir) -> None:
    """Make pystray's GTK/AppIndicator backend write its icon to a real
    ``.png`` file instead of ``tempfile.mktemp()`` (extensionless).

    libayatana-appindicator passes the icon's directory to KDE as an
    icon-theme path and the file's *basename* as an icon name; KDE then
    looks for ``<name>.png`` / ``<name>.svg`` there. pystray's default
    extensionless temp path means KDE finds nothing and shows a generic
    square fallback (GNOME reads the pixmap directly, so it only breaks on
    KDE/StatusNotifier). Writing a ``.png`` in a stable dir fixes it.
    """
    if sys.platform != "linux":
        return
    try:
        from pystray._util import gtk as _gtk
    except Exception:
        return

    import os

    os.makedirs(icon_dir, exist_ok=True)

    def _update_fs_icon(self) -> None:  # noqa: ANN001 - matches pystray signature
        # Stable per-indicator filename with a .png extension so KDE's
        # themed-icon lookup (name = basename sans extension, path = dir)
        # resolves the file.
        path = os.path.join(icon_dir, f"argus-tray-{id(self)}.png")
        self.icon.save(path, "PNG")
        self._icon_path = path
        self._icon_valid = True

    _gtk.GtkIcon._update_fs_icon = _update_fs_icon


def _wait_for_status_notifier(timeout: float = 60.0) -> bool:
    """Block until a StatusNotifier host (KDE/GNOME system-tray watcher) owns
    its bus name, so the AppIndicator icon registers against a live tray.

    On autostart at login the tray process can start before Plasma's
    StatusNotifierWatcher is up; registering then leaves a dead fallback
    (square) icon with no working menu. Poll the session bus until the
    watcher appears. Linux-only (needs GI/D-Bus); returns True immediately
    elsewhere or if the check can't run (best-effort, never blocks forever).
    """
    if sys.platform != "linux":
        return True
    try:
        import gi

        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib
    except Exception:
        return True  # can't check — proceed and hope the host is up

    watchers = ("org.kde.StatusNotifierWatcher", "org.freedesktop.StatusNotifierWatcher")
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except Exception:
        return True
    deadline = timeout
    waited = 0.0
    while waited < deadline:
        for name in watchers:
            try:
                res = bus.call_sync(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "NameHasOwner",
                    GLib.Variant("(s)", (name,)),
                    GLib.VariantType("(b)"),
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None,
                )
                if res.unpack()[0]:
                    return True
            except Exception:
                pass
        time.sleep(1.0)
        waited += 1.0
    logger.warning("StatusNotifier host not found after %.0fs; starting anyway", timeout)
    return False


def _make_icon_image(color: str) -> Image.Image:
    """Small generated dot icon — green=running, yellow=paused, gray=down.
    Avoids needing to ship an icon asset file."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 6
    draw.ellipse((pad, pad, size - pad, size - pad), fill=color)
    return img


_ICON_RUNNING = _make_icon_image("#2ecc71")
_ICON_PAUSED = _make_icon_image("#f1c40f")
_ICON_DOWN = _make_icon_image("#95a5a6")


def run_tray(config: Config) -> None:
    import pystray
    from pystray import MenuItem as Item

    # Must run before creating the Icon: fixes the KDE "generic square"
    # fallback caused by pystray writing an extensionless icon temp file.
    _patch_pystray_icon_extension(config.data_dir / "tray-icons")

    socket_path = default_socket_path(config.data_dir)
    port = config.get("dashboard", "port", default=8477)

    state = {"running": False, "paused": False}

    def _refresh_state() -> None:
        try:
            data = send_command("status", socket_path=socket_path, timeout=1.0)
            state["running"] = True
            state["paused"] = bool(data.get("paused"))
        except (IPCError, OSError, ValueError):
            state["running"] = False
            state["paused"] = False

    def _status_label(item=None) -> str:
        if not state["running"]:
            return "Argus: daemon not running"
        return f"Argus: {'PAUSED' if state['paused'] else 'running'}"

    def _pause_resume_label(item=None) -> str:
        if not state["running"]:
            return "Pause/Resume (unavailable)"
        return "Resume" if state["paused"] else "Pause"

    def _pause_resume_enabled(item=None) -> bool:
        return state["running"]

    def _open_dashboard(icon, item) -> None:
        url = f"http://127.0.0.1:{port}"
        # webbrowser.open is unreliable when $BROWSER is set to an empty
        # string (registers a no-op GenericBrowser), so open via the OS
        # handler directly on each platform and fall back to webbrowser.
        try:
            if sys.platform == "win32":
                import os

                os.startfile(url)  # noqa: S606 - Windows URL handler
            elif sys.platform == "darwin":
                subprocess.Popen(["open", url])
            else:
                subprocess.Popen(["xdg-open", url])
        except OSError:
            webbrowser.open(url)

    def _toggle_pause(icon, item) -> None:
        if not state["running"]:
            return
        cmd = "resume" if state["paused"] else "pause"
        try:
            send_command(cmd, socket_path=socket_path, timeout=2.0)
        except (IPCError, OSError, ValueError):
            logger.exception("Failed to send %s command", cmd)
        _refresh_state()
        icon.update_menu()
        icon.icon = _current_icon()

    def _quit(icon, item) -> None:
        if state["running"]:
            try:
                send_command("quit", socket_path=socket_path, timeout=2.0)
            except (IPCError, OSError, ValueError):
                logger.exception("Failed to send quit command")
        icon.stop()

    def _current_icon() -> Image.Image:
        if not state["running"]:
            return _ICON_DOWN
        return _ICON_PAUSED if state["paused"] else _ICON_RUNNING

    icon = pystray.Icon(
        "argus",
        icon=_current_icon(),
        title="Argus",
        menu=pystray.Menu(
            Item(_status_label, None, enabled=False),
            pystray.Menu.SEPARATOR,
            Item(_pause_resume_label, _toggle_pause, enabled=_pause_resume_enabled),
            # default=True → this is the action fired on a plain (left-click)
            # activate of the tray icon on KDE/StatusNotifier, where the full
            # menu is otherwise only reachable via right-click.
            Item("Open dashboard", _open_dashboard, default=True),
            pystray.Menu.SEPARATOR,
            Item("Quit Argus", _quit),
        ),
    )

    def _poll_loop() -> None:
        while True:
            time.sleep(_POLL_INTERVAL_SECONDS)
            if not icon.visible:
                return
            prev = dict(state)
            _refresh_state()
            if state != prev:
                icon.icon = _current_icon()
                icon.update_menu()

    # Wait for the desktop's system-tray host before registering, or the
    # AppIndicator icon shows a dead fallback with no menu (autostart race).
    _wait_for_status_notifier()

    _refresh_state()
    icon.icon = _current_icon()
    threading.Thread(target=_poll_loop, daemon=True).start()
    icon.run()
