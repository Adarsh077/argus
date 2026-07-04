"""Always-on service install/uninstall/status (Phase 9).

Linux (systemd user unit) and Windows (Task Scheduler) implementations.
This module only wires up the OS-native "keep it running, restart on
crash, start at login/graphical-session" mechanism — it does not implement
the tray or dashboard (later phase) or the polished installers/PKGBUILD/
.exe (final packaging phase). Packaging will later invoke `argus service
install` (or replicate its logic) as part of a full install.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("argus.service")

SERVICE_NAME = "argus.service"
TASK_NAME = "Argus"

_UNIT_TEMPLATE = """\
[Unit]
Description=Argus — always-on activity tracker
# Tied to the graphical session (not default.target): screen capture on
# KDE-Wayland goes through xdg-desktop-portal + PipeWire and active-window
# queries need the compositor's D-Bus/Wayland socket, both of which only
# exist while a graphical session is running.
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=always
RestartSec=5
# No WAYLAND_DISPLAY/DBUS_SESSION_BUS_ADDRESS/XDG_RUNTIME_DIR/DISPLAY here
# on purpose — see packaging/arch/argus.service for the full rationale.
# This unit relies on the systemd --user manager's own environment
# already carrying them (typical on modern KDE Plasma + SDDM). If capture
# fails for lack of these vars, fix it at the session level:
#   systemctl --user import-environment WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR DISPLAY

[Install]
WantedBy=graphical-session.target
"""


def _resolved_argus_command() -> str:
    """Best-effort path to the `argus` entry point that should be run by
    the service — prefer an `argus` script next to the current
    interpreter (the uv/venv case), else fall back to
    `<python> -m argus.cli run`."""
    python_dir = Path(sys.executable).parent
    candidate = python_dir / "argus"
    if candidate.exists():
        return f"{candidate} run"
    found = shutil.which("argus")
    if found:
        return f"{found} run"
    return f"{sys.executable} -m argus.cli run"


# ------------------------------------------------------------------
# Linux (systemd --user)
# ------------------------------------------------------------------


def _systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _unit_path() -> Path:
    return _systemd_user_dir() / SERVICE_NAME


def _run_systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["systemctl", "--user", *args]
    logger.info("running: %s", " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def install_linux() -> int:
    if shutil.which("systemctl") is None:
        print("systemctl not found — this Linux system doesn't appear to use systemd. Cannot install the service.")
        return 1

    exec_start = _resolved_argus_command()
    unit_dir = _systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_path = _unit_path()
    unit_path.write_text(_UNIT_TEMPLATE.format(exec_start=exec_start))
    print(f"Wrote {unit_path}")
    print(f"  ExecStart={exec_start}")
    print("  (packaging will replace this with the installed binary path)")

    _run_systemctl("daemon-reload")
    print("Ran: systemctl --user daemon-reload")

    _run_systemctl("enable", "--now", SERVICE_NAME)
    print(f"Ran: systemctl --user enable --now {SERVICE_NAME}")

    status = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE_NAME],
        capture_output=True, text=True,
    )
    print(f"Service state: {status.stdout.strip() or status.stderr.strip()}")
    return 0


def uninstall_linux() -> int:
    if shutil.which("systemctl") is None:
        print("systemctl not found — nothing to do.")
        return 0

    _run_systemctl("disable", "--now", SERVICE_NAME, check=False)
    print(f"Ran: systemctl --user disable --now {SERVICE_NAME}")

    unit_path = _unit_path()
    if unit_path.exists():
        unit_path.unlink()
        print(f"Removed {unit_path}")
    else:
        print(f"{unit_path} not present (already removed)")

    _run_systemctl("daemon-reload", check=False)
    print("Ran: systemctl --user daemon-reload")
    return 0


def start_linux() -> int:
    if shutil.which("systemctl") is None:
        print("systemctl not found — cannot start the service.")
        return 1
    # Reload first so a freshly package-installed unit (dropped under
    # /usr/lib/systemd/user by pacman, which can't run `systemctl --user`)
    # is visible to the running user manager. Idempotent + cheap.
    _run_systemctl("daemon-reload", check=False)
    # Don't gate on the ~/.config unit path: a package install ships the
    # unit under /usr/lib/systemd/user, so let systemctl resolve it and
    # surface "unit not found" itself.
    result = _run_systemctl("start", SERVICE_NAME, check=False)
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip()
        print(msg or f"failed to start {SERVICE_NAME} (is it installed?)")
        return result.returncode
    state = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE_NAME], capture_output=True, text=True
    )
    print(f"Started {SERVICE_NAME}: {state.stdout.strip() or state.stderr.strip()}")
    return 0


def stop_linux() -> int:
    if shutil.which("systemctl") is None:
        print("systemctl not found — cannot stop the service.")
        return 1
    result = _run_systemctl("stop", SERVICE_NAME, check=False)
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip())
        return result.returncode
    print(f"Stopped {SERVICE_NAME}")
    return 0


def status_linux() -> int:
    unit_path = _unit_path()
    print(f"Unit file: {unit_path} ({'present' if unit_path.exists() else 'not installed'})")
    if shutil.which("systemctl") is None:
        print("systemctl not found.")
        return 1
    result = subprocess.run(
        ["systemctl", "--user", "status", SERVICE_NAME, "--no-pager"],
        capture_output=True, text=True,
    )
    print(result.stdout or result.stderr)
    return 0


# ------------------------------------------------------------------
# Windows (Task Scheduler)
# ------------------------------------------------------------------

_TASK_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Argus - always-on activity tracker (starts at logon, restarts on crash)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _windows_pythonw_command() -> tuple[str, str]:
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    command = str(pythonw) if pythonw.exists() else str(exe)
    return command, "-m argus.cli run"


def install_windows() -> int:
    print("UNTESTED on real Windows — verify before relying on this.")
    command, arguments = _windows_pythonw_command()
    xml_path = Path.home() / "AppData" / "Local" / "Argus" / "Argus.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(
        _TASK_XML_TEMPLATE.format(command=command, arguments=arguments),
        encoding="utf-16",
    )
    print(f"Wrote {xml_path}")
    cmd = ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"]
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout or result.stderr)
    return result.returncode


def uninstall_windows() -> int:
    subprocess.run(["schtasks", "/End", "/TN", TASK_NAME], capture_output=True, text=True)
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True
    )
    print(result.stdout or result.stderr)
    return result.returncode


def status_windows() -> int:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"],
        capture_output=True, text=True,
    )
    print(result.stdout or result.stderr)
    return result.returncode if result.returncode else 0


def start_windows() -> int:
    result = subprocess.run(
        ["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True, text=True
    )
    print(result.stdout or result.stderr)
    return result.returncode


def stop_windows() -> int:
    result = subprocess.run(
        ["schtasks", "/End", "/TN", TASK_NAME], capture_output=True, text=True
    )
    print(result.stdout or result.stderr)
    return result.returncode


# ------------------------------------------------------------------
# Dispatch
# ------------------------------------------------------------------


def install() -> int:
    system = platform.system()
    if system == "Linux":
        return install_linux()
    if system == "Windows":
        return install_windows()
    print(f"argus service install: unsupported platform {system!r} (only Linux/systemd and Windows are supported)")
    return 1


def uninstall() -> int:
    system = platform.system()
    if system == "Linux":
        return uninstall_linux()
    if system == "Windows":
        return uninstall_windows()
    print(f"argus service uninstall: unsupported platform {system!r}")
    return 1


def status() -> int:
    system = platform.system()
    if system == "Linux":
        return status_linux()
    if system == "Windows":
        return status_windows()
    print(f"argus service status: unsupported platform {system!r}")
    return 1


def start() -> int:
    system = platform.system()
    if system == "Linux":
        return start_linux()
    if system == "Windows":
        return start_windows()
    print(f"argus service start: unsupported platform {system!r}")
    return 1


def stop() -> int:
    system = platform.system()
    if system == "Linux":
        return stop_linux()
    if system == "Windows":
        return stop_windows()
    print(f"argus service stop: unsupported platform {system!r}")
    return 1
