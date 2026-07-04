"""View-first local web dashboard.

Decoupling from the capture loop / daemon
------------------------------------------
This app never talks to a running daemon process. It:
  * reads the sqlite DB directly via ``argus.db.Database`` (WAL mode lets
    it read concurrently with the daemon writing),
  * reads image files directly off disk under ``config.images_dir``,
  * reuses ``argus.reports.rollup`` for session aggregation (pure DB
    reads), and
  * reuses ``argus.reports.daily`` / ``argus.reports.weekly`` for the only
    write path it has: on-demand report generation, persisted through
    their existing ``generate_and_persist_*`` functions (which in turn use
    ``Database.upsert_report``).

Capture keeps running (or not) completely independently of whether this
process is up. The only place this app talks to a running daemon is the
``/control/*`` routes, which send tiny commands over the local IPC control
channel (``argus.ipc``) — status/pause/resume. That is the one exception
allowed by the spec (UI §88): control commands, never a read path around
the database.

Security: bound to 127.0.0.1 only (enforced by the CLI's uvicorn.run
call), no auth (single-user, localhost, disk-encryption trust model per
spec). The one place that touches arbitrary-looking user input against the
filesystem is ``/image``, which is defended against path traversal (see
that route for details).
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import markdown as markdown_lib
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from fastapi.responses import JSONResponse

from argus.config import Config, load_config, save_config
from argus.db import Database
from argus.ipc import IPCError, default_socket_path, send_command
from argus.reports.daily import generate_and_persist_daily_report
from argus.reports.rollup import compute_daily_rollup
from argus.reports.weekly import generate_and_persist_weekly_report

logger = logging.getLogger("argus.dashboard")

# Resolve the templates dir relative to the actual on-disk package
# location in both dev and PyInstaller-frozen contexts. In a PyInstaller
# onedir build, `datas=[...]` copies this package's templates/ next to the
# frozen modules under `sys._MEIPASS`, but the frozen loader does not
# always leave `__file__` pointing at a real path on disk the way it does
# for a normal Python install — so prefer `sys._MEIPASS` when present
# (set only inside a frozen app) and fall back to `Path(__file__).parent`
# for normal `pip`/`uv` installs and editable/dev runs.
if getattr(sys, "_MEIPASS", None):
    TEMPLATES_DIR = Path(sys._MEIPASS) / "argus" / "dashboard" / "templates"
    STATIC_DIR = Path(sys._MEIPASS) / "argus" / "dashboard" / "static"
else:
    TEMPLATES_DIR = Path(__file__).parent / "templates"
    STATIC_DIR = Path(__file__).parent / "static"


def _fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _fmt_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _parse_date(value: str | None) -> date:
    if not value:
        return date.today()
    return datetime.strptime(value, "%Y-%m-%d").date()


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    db = Database(config.db_path)
    images_root = config.images_dir.resolve()
    recordings_root = config.recordings_dir.resolve()

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    app = FastAPI(title="Argus Dashboard")
    app.state.config = config
    app.state.db = db

    # Vendored static assets (video.js) served locally — no CDN, works on
    # the offline frozen build. Mounted only if the directory exists.
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # -- helpers --------------------------------------------------------

    def _safe_image_path(raw_path: str) -> Path | None:
        """Resolve ``raw_path`` and verify it is a regular file located
        under the configured images root. Returns None (caller must 404)
        if anything about the request looks like a traversal attempt, is
        missing, or is not a regular file. This check happens BEFORE any
        file is opened.
        """
        try:
            candidate = Path(raw_path).resolve()
        except (OSError, ValueError):
            return None

        try:
            candidate.relative_to(images_root)
        except ValueError:
            return None

        if not candidate.is_file():
            return None

        return candidate

    def _safe_recording_path(raw_path: str) -> Path | None:
        """Path-traversal-guarded resolve of a recording file under the
        recordings root. Same defense as _safe_image_path. 404 on any miss."""
        try:
            candidate = Path(raw_path).resolve()
        except (OSError, ValueError):
            return None
        try:
            candidate.relative_to(recordings_root)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        return candidate

    def _captures_for_day(day: date, capture_type: str) -> list[dict]:
        from argus.reports.rollup import local_day_range_utc

        start_utc, end_utc = local_day_range_utc(day)
        start_iso, end_iso = start_utc.isoformat(), end_utc.isoformat()
        if capture_type == "camera":
            rows = db.list_camera_frames_between(start_iso, end_iso)
        else:
            rows = db.list_screenshots_between(start_iso, end_iso)

        out = []
        for row in rows:
            ts_iso, path = row[1], row[2]
            ts = datetime.fromisoformat(ts_iso)
            ev = db.nearest_window_event(ts_iso)
            label = None
            if ev is not None:
                app_name, title = ev[1], ev[2]
                label = f"{app_name} — {title}" if title else app_name
            out.append(
                {
                    "ts": ts_iso,
                    "local_time": _fmt_local(ts),
                    "path": path,
                    "label": label,
                }
            )
        return out

    # -- routes -----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        today = date.today()
        rollup = compute_daily_rollup(db, today)
        per_app = sorted(rollup.per_app_seconds.items(), key=lambda kv: kv[1], reverse=True)
        per_app_fmt = [(app_name, _fmt_duration(secs)) for app_name, secs in per_app]

        recent = _captures_for_day(today, "screenshot")[:6]
        recent_captures = [
            {
                "path": c["path"],
                "local_time": c["local_time"],
                "type": "screenshot",
                "date": today.isoformat(),
            }
            for c in recent
        ]

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "today": today.isoformat(),
                "total_duration": _fmt_duration(rollup.total_seconds),
                "session_count": len(rollup.sessions),
                "event_count": rollup.event_count,
                "per_app": per_app_fmt,
                "recent_captures": recent_captures,
            },
        )

    @app.get("/timeline", response_class=HTMLResponse)
    def timeline(request: Request, date: str | None = None):
        day = _parse_date(date)
        rollup = compute_daily_rollup(db, day)
        sessions = [
            {
                "start_local": _fmt_local(s.start),
                "end_local": _fmt_local(s.end),
                "duration": _fmt_duration(s.duration_seconds),
                "app": s.app,
                "titles": ", ".join(sorted(s.titles)) if s.titles else "(no title)",
            }
            for s in rollup.sessions
        ]
        return templates.TemplateResponse(
            request,
            "timeline.html",
            {
                "date": day.isoformat(),
                "sessions": sessions,
                "total_duration": _fmt_duration(rollup.total_seconds),
                "event_count": rollup.event_count,
            },
        )

    @app.get("/captures", response_class=HTMLResponse)
    def captures(request: Request, date: str | None = None, type: str = "screenshot", q: str | None = None):
        day = _parse_date(date)
        capture_type = type if type in ("screenshot", "camera") else "screenshot"
        items = _captures_for_day(day, capture_type)
        if q:
            needle = q.lower()
            items = [c for c in items if c["label"] and needle in c["label"].lower()]
        return templates.TemplateResponse(
            request,
            "captures.html",
            {
                "date": day.isoformat(),
                "type": capture_type,
                "q": q,
                "captures": items,
            },
        )

    @app.get("/image")
    def image(path: str):
        safe_path = _safe_image_path(path)
        if safe_path is None:
            # Never leak *why* (missing vs traversal vs not-a-file) via a
            # different status code — always a plain 404.
            raise StarletteHTTPException(status_code=404)
        return FileResponse(safe_path, media_type="image/webp")

    @app.get("/recordings", response_class=HTMLResponse)
    def recordings_page(request: Request):
        rows = db.list_recordings()
        items = []
        for row in rows:
            _id, ts_iso, ts_end_iso, path, monitors, duration = row
            ts = datetime.fromisoformat(ts_iso)
            items.append(
                {
                    "local_time": _fmt_local(ts),
                    "path": path,
                    "monitors": monitors,
                    "duration": _fmt_duration(duration) if duration else "—",
                }
            )
        return templates.TemplateResponse(
            request, "recordings.html", {"recordings": items}
        )

    @app.get("/recording")
    def recording(path: str):
        # FileResponse honours Range requests (starlette), so the <video>
        # player can seek. Path-traversal guarded to the recordings root.
        safe_path = _safe_recording_path(path)
        if safe_path is None:
            raise StarletteHTTPException(status_code=404)
        return FileResponse(safe_path, media_type="video/mp4")

    @app.get("/reports", response_class=HTMLResponse)
    def reports_list(request: Request):
        rows = db.list_reports()
        reports = [
            {"type": r[1], "period_key": r[2], "generated_at": r[3]}
            for r in rows
        ]
        return templates.TemplateResponse(request, "reports.html", {"reports": reports})

    @app.get("/report/{report_type}/{period_key}", response_class=HTMLResponse)
    def report_detail(request: Request, report_type: str, period_key: str):
        row = db.get_report(report_type, period_key)
        if row is None:
            raise StarletteHTTPException(status_code=404, detail="Report not found")
        body_markdown = row[5]
        body_html = markdown_lib.markdown(body_markdown, extensions=["extra"])
        return templates.TemplateResponse(
            request,
            "report_detail.html",
            {
                "report_type": report_type,
                "period_key": period_key,
                "generated_at": row[3],
                "body_html": body_html,
            },
        )

    @app.post("/generate")
    def generate(date: str = Form(...), report_type: str = Form(...), vision: str | None = Form(None)):
        day = _parse_date(date)
        vision_flag = bool(vision)
        if report_type == "weekly":
            report, _row_id = generate_and_persist_weekly_report(
                db, day, config=config, vision=vision_flag
            )
            period_key = report.week_key
        else:
            report, _row_id = generate_and_persist_daily_report(
                db, day, config=config, vision=vision_flag
            )
            period_key = report.day.isoformat()
        return RedirectResponse(url=f"/report/{report_type}/{period_key}", status_code=303)

    # -- daemon control (IPC only; dashboard stays decoupled from capture) --
    _socket_path = default_socket_path(config.data_dir)

    @app.get("/control/status")
    def control_status():
        try:
            data = send_command("status", socket_path=_socket_path, timeout=1.5)
        except IPCError:
            return JSONResponse({"running": False})
        return JSONResponse({"running": True, **data})

    @app.post("/control/pause")
    def control_pause():
        try:
            send_command("pause", socket_path=_socket_path, timeout=2.0)
        except IPCError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
        return JSONResponse({"ok": True, "paused": True})

    @app.post("/control/resume")
    def control_resume():
        try:
            send_command("resume", socket_path=_socket_path, timeout=2.0)
        except IPCError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
        return JSONResponse({"ok": True, "paused": False})

    @app.post("/control/start_recording")
    def control_start_recording():
        # start_recording blocks on the daemon until the pipeline is PLAYING
        # (or fails all-or-nothing); allow a generous client timeout.
        try:
            data = send_command("start_recording", socket_path=_socket_path, timeout=30.0)
        except IPCError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
        return JSONResponse({"ok": True, **(data or {})})

    @app.post("/control/stop_recording")
    def control_stop_recording():
        try:
            data = send_command("stop_recording", socket_path=_socket_path, timeout=30.0)
        except IPCError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
        return JSONResponse({"ok": True, **(data or {})})

    @app.get("/settings", response_class=HTMLResponse)
    def settings_form(request: Request, saved: str | None = None, error: str | None = None):
        cfg = app.state.config
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "cfg": cfg.raw,
                "config_path": str(cfg.path),
                "api_key_present": cfg.api_key() is not None,
                "saved": saved,
                "error": error,
            },
        )

    @app.post("/settings")
    def settings_save(
        request: Request,
        window_interval_seconds: str = Form(...),
        screen_interval_seconds: str = Form(...),
        camera_interval_seconds: str = Form(...),
        screen_enabled: str | None = Form(None),
        camera_enabled: str | None = Form(None),
        camera_device_index: str = Form(...),
        image_webp_quality: str = Form(...),
        data_location: str = Form(""),
        retention_days: str = Form(...),
        recordings_retention_days: str = Form(...),
        vision_provider: str = Form(...),
        vision_model: str = Form(...),
        vision_endpoint: str = Form(...),
        sampling_count: str = Form(...),
        dashboard_port: str = Form(...),
    ):
        errors: list[str] = []

        def _int(name: str, raw: str, *, min_value: int | None = None, max_value: int | None = None) -> int | None:
            try:
                v = int(raw)
            except (TypeError, ValueError):
                errors.append(f"{name} must be a whole number.")
                return None
            if min_value is not None and v < min_value:
                errors.append(f"{name} must be >= {min_value}.")
                return None
            if max_value is not None and v > max_value:
                errors.append(f"{name} must be <= {max_value}.")
                return None
            return v

        window_iv = _int("Window poll interval", window_interval_seconds, min_value=1)
        screen_iv = _int("Screen capture interval", screen_interval_seconds, min_value=1)
        camera_iv = _int("Camera capture interval", camera_interval_seconds, min_value=1)
        camera_idx = _int("Camera device index", camera_device_index, min_value=0)
        quality = _int("Image quality", image_webp_quality, min_value=1, max_value=100)
        retention = _int("Retention days", retention_days, min_value=0)
        rec_retention = _int("Recordings retention days", recordings_retention_days, min_value=0)
        sampling = _int("Sampling count", sampling_count, min_value=0)
        port = _int("Dashboard port", dashboard_port, min_value=1, max_value=65535)

        provider = vision_provider.strip()
        model = vision_model.strip()
        endpoint = vision_endpoint.strip()
        if not provider:
            errors.append("Vision provider must not be empty.")
        if not model:
            errors.append("Vision model must not be empty.")
        if not endpoint:
            errors.append("Vision endpoint must not be empty.")

        data_loc = data_location.strip()

        if errors:
            cfg = app.state.config
            return templates.TemplateResponse(
                request,
                "settings.html",
                {
                    "cfg": cfg.raw,
                    "config_path": str(cfg.path),
                    "api_key_present": cfg.api_key() is not None,
                    "saved": None,
                    "error": " ".join(errors),
                },
                status_code=400,
            )

        updates = {
            "capture": {
                "window_interval_seconds": window_iv,
                "screen_interval_seconds": screen_iv,
                "camera_interval_seconds": camera_iv,
                "screen_enabled": bool(screen_enabled),
                "camera_enabled": bool(camera_enabled),
                "camera_device_index": camera_idx,
                "image_webp_quality": quality,
            },
            "storage": {
                "data_location": data_loc,
                "retention_days": retention,
                "recordings_retention_days": rec_retention,
            },
            "vision": {
                "provider": provider,
                "model": model,
                "endpoint": endpoint,
                "sampling_count": sampling,
            },
            "dashboard": {
                "port": port,
            },
        }
        app.state.config = save_config(app.state.config, updates)
        return RedirectResponse(url="/settings?saved=1", status_code=303)

    return app
