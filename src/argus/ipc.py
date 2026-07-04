"""Local control channel for the Argus daemon.

Transport
---------
Linux/macOS: a Unix domain socket at ``$XDG_RUNTIME_DIR/argus.sock`` (falls
back to the data dir if XDG_RUNTIME_DIR is unset), created with 0600
permissions.
Windows: no Unix sockets, so we use a TCP socket bound to
``127.0.0.1:<ARGUS_IPC_PORT>`` (fixed port, default 8478). Loopback-only —
never binds 0.0.0.0.

Protocol
--------
Tiny newline-delimited JSON request/response, one exchange per connection:
    request:  {"cmd": "status"}
    response: {"ok": true, "data": {...}}   or   {"ok": false, "error": "..."}

Commands: ping, status, pause, resume, quit.

Security
--------
Local-only, no auth — the Unix socket's filesystem permissions (0600, owned
by the invoking user) and the fact that TCP is bound to loopback only are
the entire trust boundary, matching the rest of Argus's single-user,
localhost trust model. This is NOT designed to be exposed beyond localhost.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import sys
import threading
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("argus.ipc")

IS_WINDOWS = sys.platform == "win32"
DEFAULT_TCP_PORT = 8478
_RECV_CHUNK = 4096
_MAX_MESSAGE_BYTES = 1 << 16  # 64 KiB is plenty for this tiny protocol


def default_socket_path(data_dir: Path | None = None) -> Path:
    """Unix domain socket path. Prefers $XDG_RUNTIME_DIR (which is normally
    tmpfs, per-user, mode 0700) and falls back to the Argus data dir."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "argus.sock"
    if data_dir is not None:
        return data_dir / "argus.sock"
    return Path("/tmp") / f"argus-{os.getuid()}.sock"  # last-resort fallback


def _tcp_port() -> int:
    try:
        return int(os.environ.get("ARGUS_IPC_PORT", DEFAULT_TCP_PORT))
    except ValueError:
        return DEFAULT_TCP_PORT


def _recv_line(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = sock.recv(_RECV_CHUNK)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_MESSAGE_BYTES:
            raise ValueError("IPC message too large")
        if b"\n" in chunk:
            break
    return b"".join(chunks)


class IPCServer:
    """Runs in a background thread inside the daemon process. Accepts one
    connection at a time (fine for this low-volume control protocol) and
    dispatches JSON commands to the handler map supplied by the caller.
    """

    def __init__(
        self,
        handlers: dict[str, Callable[[], Any]],
        socket_path: Path | None = None,
    ):
        self.handlers = handlers
        self.socket_path = socket_path or default_socket_path()
        self._server: socketserver.BaseServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        handlers = self.handlers
        socket_path = self.socket_path

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    raw = _recv_line(self.request)
                    if not raw:
                        return
                    req = json.loads(raw.decode("utf-8"))
                    cmd = req.get("cmd")
                    fn = handlers.get(cmd)
                    if fn is None:
                        resp: dict[str, Any] = {"ok": False, "error": f"unknown command: {cmd!r}"}
                    else:
                        data = fn()
                        resp = {"ok": True, "data": data}
                except Exception as exc:  # noqa: BLE001 - always report back to client
                    logger.exception("IPC request failed")
                    resp = {"ok": False, "error": str(exc)}
                try:
                    self.request.sendall((json.dumps(resp) + "\n").encode("utf-8"))
                except OSError:
                    pass

        if IS_WINDOWS:
            class _Server(socketserver.ThreadingTCPServer):
                allow_reuse_address = True
                daemon_threads = True

            self._server = _Server(("127.0.0.1", _tcp_port()), _Handler)
        else:
            self.socket_path.parent.mkdir(parents=True, exist_ok=True)
            if self.socket_path.exists():
                # Stale socket from a previous unclean shutdown.
                try:
                    self.socket_path.unlink()
                except OSError:
                    pass

            class _Server(socketserver.ThreadingUnixStreamServer):
                daemon_threads = True

            self._server = _Server(str(self.socket_path), _Handler)
            os.chmod(self.socket_path, 0o600)

        self._thread = threading.Thread(target=self._server.serve_forever, name="argus-ipc", daemon=True)
        self._thread.start()
        logger.info("IPC server listening on %s", self.socket_path if not IS_WINDOWS else f"127.0.0.1:{_tcp_port()}")

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if not IS_WINDOWS and self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass


class IPCError(RuntimeError):
    """Raised by the client when the daemon is unreachable or returns an error."""


def send_command(cmd: str, *, socket_path: Path | None = None, timeout: float = 2.0) -> Any:
    """Client helper: connect, send one JSON command, return its ``data``.

    Raises IPCError if the daemon isn't running (socket/port unreachable)
    or if the daemon reports an error.
    """
    payload = (json.dumps({"cmd": cmd}) + "\n").encode("utf-8")

    if IS_WINDOWS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        address: Any = ("127.0.0.1", _tcp_port())
    else:
        path = socket_path or default_socket_path()
        if not path.exists():
            raise IPCError("daemon not running (no socket found)")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        address = str(path)

    sock.settimeout(timeout)
    try:
        try:
            sock.connect(address)
        except OSError as exc:
            raise IPCError("daemon not running (connection refused)") from exc
        sock.sendall(payload)
        raw = _recv_line(sock)
    finally:
        sock.close()

    if not raw:
        raise IPCError("daemon closed connection without a response")
    resp = json.loads(raw.decode("utf-8"))
    if not resp.get("ok"):
        raise IPCError(resp.get("error", "unknown IPC error"))
    return resp.get("data")


def is_daemon_running(*, socket_path: Path | None = None, timeout: float = 1.0) -> bool:
    try:
        send_command("ping", socket_path=socket_path, timeout=timeout)
        return True
    except (IPCError, OSError, ValueError):
        return False
