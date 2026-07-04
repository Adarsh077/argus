# Argus

**Argus** (after the hundred-eyed, ever-watchful giant of Greek myth) is a
personal, always-on desktop activity/productivity tracker for CachyOS (KDE
Plasma, Wayland) and Windows.

This repo currently implements **Phase 1** of the build plan: a daemon
skeleton, config, and database, with both capture loops writing records
using stubbed capturers. Real window/screen/camera capture, time rollups,
reports, retention, the web dashboard, and the system tray are later
phases — not in this build.

## Install

Requires [`uv`](https://docs.astral.sh/uv/).

```sh
uv sync
```

## Usage

```sh
uv run argus status   # show detected platform/backend, config path, db path, row counts
uv run argus run      # run the daemon in the foreground (Ctrl-C to stop)
```

Other subcommands (`report`, `pause`, `resume`, `dashboard`,
`install-service`) exist as placeholders that print "not implemented" —
they land in later phases.

On first run, `argus` creates a default config file if one doesn't exist
yet, and creates the SQLite database on first capture.

## Configuration

Config is a TOML file at (per-user config directory via `platformdirs`):

- Linux: `~/.config/argus/config.toml`
- Windows: `%APPDATA%\argus\config.toml`

Keys:

```toml
[capture]
window_interval_seconds = 5     # active-window capture interval
screen_interval_seconds = 300   # screenshot capture interval
camera_interval_seconds = 300   # camera capture interval
screen_enabled = true
camera_enabled = true
camera_device_index = 0
image_webp_quality = 80

[storage]
data_location = ""              # empty = default platformdirs data dir
retention_days = 30

[vision]
provider = "google"
model = "gemini-2.5-flash-lite"
endpoint = "https://generativelanguage.googleapis.com/v1beta/models"
sampling_count = 3

[report]
daily_schedule = "23:55"
weekly_schedule = "SUN 23:59"
```

**The vision API key is never read from or stored in this file.** It is
read only from the environment: `GEMINI_API_KEY` (checked first) or
`GOOGLE_API_KEY`.

Data (SQLite database + images) lives under the per-user data directory by
default:

- Linux: `~/.local/share/argus/`
- Windows: `%LOCALAPPDATA%\argus\`

## Database

SQLite, WAL mode, timezone-aware UTC timestamps. Tables (Phase 1):

- `window_events (id, ts, app, window_title, monitor)`
- `screenshots (id, ts, path, monitors)`
- `camera_frames (id, ts, path, device)`

## What's stubbed in Phase 1

- **Window capture** writes `app="stub"`, `window_title="stub window"` on
  every tick — no real active-window query yet.
- **Screen and camera capture** write a tiny placeholder WebP image (not a
  real screenshot or camera frame) and a real DB row referencing it.

Every loop iteration writes a real row, so the daemon → DB pipeline is
fully verifiable end-to-end before any real capture backend exists.

## Accepted risks — do not "fix" without asking

These are deliberate product decisions, carried over from the spec:

1. **Cloud upload, no filtering.** Sampled screenshots go to a cloud
   vision API with no exclude-list, redaction, or pause control beyond the
   pause switch below. They will routinely contain passwords, messages,
   2FA codes, banking and client data, all leaving the machine into a
   third party's logs. Chosen deliberately.
2. **No idle detection.** Time counts wall-clock presence, not true
   activity; overcounts when away.
3. **Full-res camera every 5 minutes, kept 30 days.** Weak productivity
   signal, large privacy surface; retained per policy.

**Revised risk note:** once the pause control (tray/dashboard, later
phase) exists, accepted risk #1 is only *partially* mitigated — there is
still no per-app exclude-list and no redaction. While running, everything
is captured and sampled screenshots are uploaded unfiltered. **Pause is
the only gate.**

In this Phase 1 build, the pause mechanism is only an internal flag on the
`Daemon` object (`daemon.pause()` / `daemon.resume()`) checked by each
capture loop — there is no external control channel (tray/CLI/dashboard)
wired up yet.

## Platform detection

`argus status` (and the daemon on startup) detects:

- OS: Linux vs Windows (`platform.system()`).
- On Linux: session type via `XDG_SESSION_TYPE` and desktop via
  `XDG_CURRENT_DESKTOP`, to identify KDE-Wayland specifically.

Only **KDE-Wayland** and **Windows** are in scope for v1. Any other
combination (X11, GNOME, macOS, ...) logs a clear warning and falls back
to an "unsupported" backend id so the daemon can still run locally with
stub capturers.

## Project layout

```
src/argus/
  cli.py                 # argus run / status / (stubs for later commands)
  config.py              # TOML config load/merge, defaults, API key from env only
  db.py                  # SQLite schema + access, WAL mode, UTC timestamps
  daemon.py              # orchestrates the three capture loops + pause flag
  capture/
    base.py              # abstract Capturer interface
    window.py            # active-window loop (stub)
    screen.py            # screenshot loop (stub)
    camera.py            # camera loop (stub)
    platform/
      detect.py          # OS / session / desktop detection -> backend id
```
