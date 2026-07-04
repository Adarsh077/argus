# Argus

**Argus** (after the hundred-eyed, ever-watchful giant of Greek myth) is a
personal, always-on desktop activity/productivity tracker for CachyOS (KDE
Plasma, Wayland) and Windows.

It captures active-window metadata every 5s (the backbone of time
tracking), all-monitor screenshots and a camera frame every 5min, rolls
that into per-app / per-session time, and generates daily and weekly
reports — optionally with cloud-vision session narratives. A local web
dashboard views everything; a tray icon and on-demand screen recording
round it out.

Only **KDE-Wayland** and **Windows** are supported. On any other
combination (X11, GNOME, macOS, …) the daemon still runs but capture is a
no-op (an "unsupported" backend).

---

## Install (prod)

### Arch / CachyOS — AUR

Package: [`argus-tracker`](https://aur.archlinux.org/packages/argus-tracker)
(the bare name `argus` is taken on AUR by unrelated projects; the installed
command is still `argus`).

```sh
paru -S argus-tracker      # or: yay -S argus-tracker
```

The package installs a `systemd --user` service and **enables it globally**,
so Argus starts automatically on your next graphical login. Manage it now
without logging out:

```sh
argus service start        # start the service in the current session
argus service stop         # stop it
argus service status       # show systemd unit status
```

The tray icon autostarts at graphical login. First screen capture triggers
a one-time xdg-desktop-portal ScreenCast consent dialog — approve it once,
subsequent captures are silent.

**Vision API key** (only needed for `--vision` narratives): the
`systemd --user` service does not inherit your shell env, so set it where
the user manager picks it up:

```sh
mkdir -p ~/.config/environment.d
echo 'GEMINI_API_KEY=your-key-here' >> ~/.config/environment.d/argus.conf
systemctl --user import-environment GEMINI_API_KEY   # load it into the running user manager
argus service stop
argus service start
```

### Windows

Download the `argus-setup-*.exe` from the
[latest GitHub Release](https://github.com/Adarsh077/argus/releases) and run
it. It installs a background task that starts at logon (restart-on-crash).

---

## Develop (dev)

Requires [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync                  # install deps into .venv
uv run argus status      # platform/backend, config + db paths, row counts, vision-key presence
uv run argus run         # run the daemon in the foreground (Ctrl-C to stop)
uv run argus dashboard   # local dashboard on http://127.0.0.1:8477
```

On first run Argus creates a default config file (if missing) and creates
the SQLite database on first capture.

To run as a service from a working copy instead of the foreground:

```sh
uv run argus service install    # writes+enables a systemd --user unit pointing at this venv
uv run argus service start
uv run argus service status
```

To build the Arch package from the working tree (has the latest packaging
fixes before they hit the AUR):

```sh
cd packaging/arch && makepkg -fi
```

Releases are automated from Conventional Commit messages — see
[`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Commands

```sh
argus status                  # platform/backend, config, db, row counts, vision-key presence
argus run                     # run the daemon in the foreground
argus dashboard               # local web dashboard (127.0.0.1:8477)

argus pause | resume | quit   # control a running daemon over the local IPC socket

# Reports are on-demand only — there is no scheduler.
argus report daily   [--date YYYY-MM-DD] [--vision]
argus report weekly  [--date YYYY-MM-DD] [--vision]
argus report show --type {daily|weekly} (--date YYYY-MM-DD | --period YYYY-Www)
argus report list

argus purge [--days N] [--dry-run]        # apply retention now

argus service {install|uninstall|start|stop|status}
argus tray                                # run the tray icon
```

`--vision` is **opt-in per invocation**: without it, no screenshots are
read and no network calls are made. With it, Argus samples a few
representative screenshots per session and uploads them to the configured
vision API (Gemini by default) for a short narrative. Needs a key in the
environment (see above); if absent, reports still generate — just without
narratives.

Prefix with `uv run` when running from a working copy (e.g.
`uv run argus report daily`).

---

## Configuration

TOML file at the per-user config directory (via `platformdirs`), auto-created
with defaults on first run:

- Linux: `~/.config/argus/config.toml`
- Windows: `%APPDATA%\argus\config.toml`

```toml
[capture]
window_interval_seconds = 5     # active-window capture interval
screen_interval_seconds = 300   # screenshot capture interval
camera_interval_seconds = 300   # camera capture interval
screen_enabled = true
camera_enabled = true
camera_device_index = 0
image_webp_quality = 80
recording_noise_suppression = true   # WebRTC/RNNoise mic denoise on recordings

[storage]
data_location = ""                   # empty = default platformdirs data dir
retention_days = 30                  # raw images
recordings_retention_days = 7        # screen recordings (mp4) — larger, shorter window

[vision]
provider = "google"
model = "gemini-2.5-flash-lite"
endpoint = "https://generativelanguage.googleapis.com/v1beta/models"
sampling_count = 3                   # max screenshots sampled per session

[dashboard]
port = 8477                          # bound to 127.0.0.1 only, no auth
```

**The vision API key is never read from or stored in this file.** It comes
from the environment only: `GEMINI_API_KEY` (checked first) or
`GOOGLE_API_KEY`.

Data (SQLite database + images + recordings) lives under the per-user data
directory by default:

- Linux: `~/.local/share/argus/`
- Windows: `%LOCALAPPDATA%\argus\`

---

## Database

SQLite, WAL mode (concurrent dashboard reads while the daemon writes),
timezone-aware UTC ISO-8601 timestamps. Tables:

- `window_events (id, ts, app, window_title, monitor)`
- `screenshots (id, ts, path, monitors)`
- `camera_frames (id, ts, path, device)`
- `recordings (id, ts, ts_end, path, monitors, duration_seconds)`
- `reports (id, type, period_key, generated_at, data_json, body_markdown)`

---

## Accepted risks — do not "fix" without asking

Deliberate product decisions, carried over from the spec:

1. **Cloud upload, no filtering.** Sampled screenshots sent for `--vision`
   narratives go to a cloud vision API with no exclude-list or redaction.
   They can contain passwords, messages, 2FA codes, banking and client
   data, all leaving the machine into a third party's logs. Chosen
   deliberately. Vision is opt-in per report, and **pause is the only
   gate** while running — there is no per-app exclude-list.
2. **No idle detection.** Time counts wall-clock presence, not true
   activity; overcounts when away.
3. **Full-res camera every 5 minutes, kept 30 days.** Weak productivity
   signal, large privacy surface; retained per policy.

Encryption is out of scope: Argus relies on disk-level encryption (LUKS /
BitLocker) and does not manage its own keys.

---

## Project layout

```
src/argus/
  cli.py                 # argparse entry point (argus = argus.cli:main)
  config.py              # TOML config load/merge, API key from env only
  state.py               # machine runtime state (e.g. Wayland restore_token)
  db.py                  # SQLite schema + access, WAL, UTC timestamps
  daemon.py              # orchestrates capture loops + retention + IPC + recorder
  ipc.py                 # local control channel (Unix socket / loopback TCP)
  service.py             # systemd --user unit / Windows Task Scheduler install
  tray.py                # system tray icon
  capture/
    base.py              # abstract Capturer interface
    window.py screen.py camera.py recorder.py
    platform/            # OS-specific backends (kde-wayland / windows), detect.py
  reports/               # rollup.py (sessions/time), daily.py, weekly.py
  vision/                # sampler.py (pick screenshots), client.py (Gemini)
  dashboard/             # FastAPI app + Jinja templates + static assets
```
