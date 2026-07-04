# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Argus — a personal, always-on desktop activity/productivity tracker for **KDE-Wayland (CachyOS)** and **Windows**. A headless Python daemon captures active-window metadata (every 5s), all-monitor screenshots and a camera frame (every 5min), rolls that into per-app/per-session time, and generates daily/weekly reports with optional cloud-vision session narratives. A local FastAPI dashboard views everything.

**Note:** `README.md` describes "Phase 1 / stubbed capture" and is stale. The build is well past that — real capture, IPC control, reports, vision, dashboard, tray, service install, and on-demand screen recording all exist. Trust the code (and `activity-tracker-spec.md` for intent) over the README's phase language.

## Commands

Uses [`uv`](https://docs.astral.sh/uv/). No test suite exists in-repo.

```sh
uv sync                              # install deps into .venv
uv run argus status                  # platform/backend, config/db paths, row counts, vision-key presence
uv run argus run                     # run daemon in foreground (Ctrl-C to stop)
uv run argus dashboard               # FastAPI dashboard on http://127.0.0.1:8477
uv run argus report daily [--date YYYY-MM-DD] [--vision]   # generate+persist+print
uv run argus report weekly [--date YYYY-MM-DD] [--vision]
uv run argus report show --type {daily|weekly} (--date|--period)
uv run argus report list
uv run argus purge [--days N] [--dry-run]
uv run argus pause | resume | quit   # IPC commands to a running daemon
uv run argus tray                    # system tray
uv run argus service {install|uninstall|start|stop|status}
```

`--vision` is opt-in per invocation: without it, zero screenshots are read and no network calls happen. Vision needs `GEMINI_API_KEY` or `GOOGLE_API_KEY` in the env — never crashes if absent, just omits narratives.

## Architecture

Everything is single-user, localhost trust model (no auth anywhere); disk encryption is assumed.

- **`daemon.py`** — orchestrates three independent capture loops (window/screen/camera) as threads on their own intervals + a once-a-day retention purge. Owns the `Recorder` (on-demand) and the `IPCServer`. `paused` is an in-memory `threading.Event` (resets to running on restart — no persistence).
- **`cli.py`** — argparse entry point (`argus = "argus.cli:main"`). Live daemon state comes via IPC; falls back to reading the DB on disk when no daemon is up.
- **`config.py`** — TOML at platformdirs config dir, auto-created with `DEFAULTS`. **API keys are read from env only, never from/into config.** `Config` exposes `db_path`, `data_dir`, `images_dir`, `api_key()`.
- **`state.py`** — machine-generated runtime state (JSON at `<data>/state.json`), distinct from user config. Holds the XDG ScreenCast `restore_token` enabling silent re-capture on Wayland.
- **`db.py`** — SQLite, WAL mode, all timestamps stored as **UTC ISO-8601 strings**. Tables: `window_events`, `screenshots`, `camera_frames`, `recordings`, `reports` (`reports` uses `UNIQUE(type, period_key)`; regenerating a report replaces the row via `upsert_report`).
- **`ipc.py`** — local control channel. Unix socket at `$XDG_RUNTIME_DIR/argus.sock` (0600); on Windows a loopback TCP socket. Newline-delimited JSON, one exchange per connection. Commands: ping/status/pause/resume/quit/start_recording/stop_recording.
- **`capture/`** — `base.Capturer` interface; `window.py`/`screen.py`/`camera.py` loops; `recorder.py` on-demand screen+audio recording. **`capture/platform/`** holds the OS-specific backends selected by `detect.py` (`kde-wayland` / `windows` / `unsupported`). Unsupported backends log once and no-op — the loop never crashes.
  - Screenshots: all monitors composited into one WebP, stored `<data>/images/screenshots/YYYY-MM-DD/<utc-ts>.webp` (day-foldered so retention purges whole dates).
  - Wayland screen capture goes through XDG ScreenCast portal + PipeWire; recording adds GStreamer audio (mic + sink monitor, mixed to AAC). Recording is all-or-nothing (video AND both audio sources, else it raises).
- **`reports/`** — `rollup.py` is pure DB-read session/time aggregation (session = contiguous same-app run; gap ≤60s absorbed, >60s or app-switch splits; per-event duration capped at 60s). `daily.py`/`weekly.py` generate+persist; weekly aggregates from already-persisted daily narratives rather than re-narrating. **Reports are on-demand only — no scheduler anywhere.**
- **`vision/`** — `sampler.py` picks ≤`sampling_count` evenly-spaced screenshots per session; `client.py` calls Gemini `generateContent`. Every network/parse failure is caught → logged → `None`; callers treat `None` as "no narrative" and continue. Only ever invoked from report generation with `--vision`.
- **`dashboard/`** — FastAPI, reads the DB and image files directly (never talks to the daemon for data — WAL allows concurrent reads). The one exception: `/control/*` routes send IPC pause/resume/status/recording commands. Bound to 127.0.0.1 only. `/image` route defends against path traversal. Jinja templates + vendored video.js.
- **`service.py`** — installs the OS keep-alive: systemd **user** unit (`PartOf=graphical-session.target`, `Restart=always`) on Linux, Task Scheduler on Windows.

## Releases (automated — do not bump versions by hand)

python-semantic-release drives everything off **Conventional Commit** messages on `main`. `feat:`→minor, `fix:`→patch, `docs/ci/chore/refactor/test/style/perf`→no release. `allow_zero_version = true` keeps it in `0.x` (breaking changes bump minor, not to 1.0). PSR stamps the version into `pyproject.toml`, `packaging/arch/PKGBUILD`, and `packaging/arch/.SRCINFO`; the Windows installer gets its version from CI. Artifacts (Arch package + Windows installer) build in `.github/workflows/release.yml` against the tag. Commit format required — see `CONTRIBUTING.md`.

## Accepted risks — do NOT "fix" without asking

Deliberate product decisions from the spec: (1) sampled screenshots upload to a cloud vision API with no exclude-list/redaction — they can contain passwords, 2FA codes, banking/client data; pause is the only gate. (2) No idle detection — time is wall-clock presence, overcounts when away. (3) Full-res camera every 5min kept 30 days. Don't add filtering/idle-detection/etc. as "fixes."
