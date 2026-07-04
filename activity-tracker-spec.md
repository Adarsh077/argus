# Argus — Requirements Spec

**Argus** is a personal, always-on desktop activity/productivity tracker. Runs on **CachyOS (KDE Plasma, Wayland)** and **Windows**.

This document states *what* to build and *what was decided*, not how. Choose implementation approaches yourself; only the decisions and constraints below are fixed.

---

## Goal

Track how time is spent at the computer and produce automatic **daily and weekly summaries**. Screenshots and camera frames are an evidence layer; continuous active-window metadata is the primary signal for time tracking; a cloud vision model turns sampled screenshots into human-readable session narratives.

## Stack

Python headless daemon, cross-platform, with a platform-specific layer for the pieces that differ between KDE-Wayland and Windows. No GUI in v1 — output is generated summary files plus a local database. Tray/UI can come later.

## What it captures

- **Active-window metadata** — every **5 seconds**: timestamp, app/process, window title, monitor. This is the backbone of time tracking.
- **Screenshot** — every **5 minutes**: all monitors, composited into one image, stored as **WebP**.
- **Camera** — every **5 minutes**: one full-resolution frame from the default camera, stored as **WebP**.
- **No idle detection** in v1 — loops fire regardless of presence.

## Platforms

- **CachyOS / KDE Plasma / Wayland.** Screen capture must work silently on Wayland with no per-capture permission prompt after initial setup. Active-window info must be read reliably on KDE-Wayland. Runs as an always-on user-level background service with restart-on-crash, starting automatically in the graphical session.
- **Windows.** Screen capture across all monitors, active-window info, camera capture. Runs as an always-on background task starting at logon, restart-on-crash.
- Detect OS (and Linux session/desktop) at startup and select the right backend. Only KDE-Wayland and Windows are in scope for v1.

## Storage

- Images stored on local disk, organized so old data is easy to purge by date.
- Metadata and derived data stored in a local structured database.
- **Retention:** raw images auto-deleted after **30 days** (configurable). Narratives and summaries kept **forever**.
- **Encryption:** rely on disk-level encryption (LUKS / BitLocker). The app does not manage its own keys in v1.

## Summaries

- **Time rollup** from active-window metadata → time-per-app and time-per-window over a period, plus session boundaries. This alone should produce a useful summary with no vision involved.
- **Vision narratives:** sample a small number of representative screenshots per session (not every screenshot) and send them to a **configurable cloud vision API** to generate short session descriptions.
- **Reports:** combine rollup + narratives into **daily** and **weekly** summaries, generated on a schedule and on demand. Summaries persist.

## Configuration

Everything tunable should live in config: capture intervals, enable/disable screen or camera, camera device, image quality, data location, retention window, vision provider/model/endpoint, sampling count, report schedule. API keys come from the environment, never hard-coded or stored in the config file.

## Accepted risks — do not "fix" without asking

These were deliberate choices. Record them in the README.

1. **Cloud upload, no filtering.** Sampled screenshots go to a cloud vision API with no exclude-list, redaction, or pause control. They will routinely contain passwords, messages, 2FA codes, banking and client data, all leaving the machine into a third party's logs. Chosen deliberately.
2. **No idle detection.** Time counts wall-clock presence, not true activity; overcounts when away.
3. **Full-res camera every 5 minutes, kept 30 days.** Weak productivity signal, large privacy surface; retained per policy.

## Build order

1. Daemon skeleton + config + database, with both capture loops writing records (stubbed capturers).
2. Active-window metadata working end-to-end on both platforms.
3. Screen capture — Windows first, then KDE-Wayland (the hardest part; expect iteration on silent Wayland capture).
4. Camera capture on both platforms.
5. Time rollup + daily report (metadata only, no vision) — backbone must be useful on its own.
6. Retention purge.
7. Screenshot sampling + cloud vision narratives.
8. Weekly report.
9. Always-on service setup on both platforms with restart-on-crash and autostart.

Usable after step 5; the rest layers on.

---

## UI (added)

Two surfaces, both cross-platform (CachyOS/KDE + Windows):

**System tray** — primary control:
- Pause / resume capture (halts both metadata and screenshot/camera/upload loops).
- Quit.
- Open dashboard.
- Show status (running / paused).

**Local web dashboard** (daemon serves a localhost page) — view + configure:
- Timeline of activity.
- Browse and search captures; view stored screenshot/camera images.
- Daily / weekly summaries.
- Trigger a report on demand.
- Edit settings (the config values).

The dashboard is decoupled from the capture loops — it reads the database/images and issues control/report commands to the daemon. Capture keeps running whether or not the dashboard is open.

### Revised accepted risk

Accepted risk #1 is now **partially mitigated**: a pause control exists, so capture and cloud upload can be stopped on demand. There is still **no per-app exclude-list and no redaction** — while running, everything is captured and sampled screenshots are uploaded unfiltered. Pause is the only gate.

### Build order additions
- After step 9 (service setup): add the **web dashboard** (view first: timeline, images, summaries, reports), then **settings editing**, then the **system tray** with pause/resume/quit/open.

---

## Distribution & install (added)

The user will **not** clone the repo or run anything manually. Ship as double-click installers that bundle their own runtime (no manual Python/venv/dependency install) and set everything up.

**Windows** — an installer `.exe` that:
- Bundles the Python runtime and all dependencies into a self-contained build.
- Installs the app.
- Registers the always-on autostart task (at logon, restart-on-crash).
- Installs the tray + dashboard.
- After install: it just runs; user does nothing else.

**CachyOS (Arch)** — a **PKGBUILD** (installable via `paru`/`pacman`, publishable to AUR) that:
- Packages the app with its dependencies (self-contained where practical).
- Declares/installs the external tools the KDE-Wayland capture needs (portal/PipeWire stack, window-query helper) as package dependencies so the user doesn't install them by hand.
- Installs and enables the always-on user service (restart-on-crash, autostart in graphical session).
- Installs the tray + dashboard.

First launch still shows the one-time KDE screen-capture consent dialog; after approval, capture is silent on subsequent runs.

Do **not** use Flatpak — the Wayland portal + PipeWire + external window-query tool + user service requirements conflict with the sandbox.

### Build order addition
- Final phase: produce the **Windows `.exe` installer** and the **CachyOS PKGBUILD**, each bundling the runtime, wiring autostart, and installing tray + dashboard — so install is double-click / one package command with no manual steps.

---

## Name

The software is called **Argus** (after the hundred-eyed, ever-watchful giant of Greek myth — the never-sleeping watchman). Use it for naming throughout:
- Command / executable: `argus`
- Daemon / service: `argus` (Linux user service `argus.service`; Windows task "Argus")
- Package: `argus`
- Data directory and config named under `argus`.
