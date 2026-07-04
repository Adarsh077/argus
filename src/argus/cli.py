"""Argus command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

from argus.capture.platform.detect import detect_platform
from argus.config import load_config
from argus.daemon import Daemon
from argus.db import Database
from argus.ipc import IPCError, send_command
from argus.reports.daily import generate_and_persist_daily_report
from argus.reports.weekly import WEEKLY_VISION_TOP_SESSIONS, generate_and_persist_weekly_report
from argus.retention import purge as run_purge
from argus import service as service_mod


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def cmd_run(args: argparse.Namespace) -> int:
    _setup_logging()
    config = load_config()
    daemon = Daemon(config)
    logging.getLogger("argus.cli").info("Starting Argus. Press Ctrl-C to stop.")
    daemon.run_forever()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    info = detect_platform()

    print("Argus status")
    print("------------")
    print(f"Platform:     {info.os_name}")
    print(f"Session type: {info.session_type or '-'}")
    print(f"Desktop:      {info.desktop or '-'}")
    print(f"Backend:      {info.backend}")
    print(f"Config path:  {config.path}")
    print(f"Data dir:     {config.data_dir}")
    print(f"DB path:      {config.db_path}")

    try:
        data = send_command("status", socket_path=_socket_path(config))
    except IPCError:
        data = None

    if data is not None:
        state = "PAUSED" if data["paused"] else "RUNNING"
        print(f"Daemon:       running ({state}), uptime {int(data['uptime_seconds'])}s")
        print("Row counts (live, via IPC):")
        for table, count in data["row_counts"].items():
            print(f"  {table:16s} {count}")
    else:
        print("Daemon:       not running")
        if config.db_path.exists():
            db = Database(config.db_path)
            counts = db.row_counts()
            db.close()
            print("Row counts (from DB on disk):")
            for table, count in counts.items():
                print(f"  {table:16s} {count}")
        else:
            print("Row counts:   (database not created yet — run `argus run` first)")

    api_key_present = config.api_key() is not None
    print(f"Vision API key set: {api_key_present}")
    return 0


def _socket_path(config):
    from argus.ipc import default_socket_path

    return default_socket_path(config.data_dir)


def cmd_pause(args: argparse.Namespace) -> int:
    config = load_config()
    try:
        send_command("pause", socket_path=_socket_path(config))
    except IPCError as exc:
        print(f"Could not pause: {exc}")
        return 1
    print("Argus paused. Capture (window/screen/camera) halted.")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    config = load_config()
    try:
        send_command("resume", socket_path=_socket_path(config))
    except IPCError as exc:
        print(f"Could not resume: {exc}")
        return 1
    print("Argus resumed. Capture running again.")
    return 0


def cmd_quit(args: argparse.Namespace) -> int:
    config = load_config()
    try:
        send_command("quit", socket_path=_socket_path(config))
    except IPCError as exc:
        print(f"Could not quit: {exc}")
        return 1
    print("Argus daemon shutting down.")
    return 0


def cmd_report_daily(args: argparse.Namespace) -> int:
    _setup_logging()
    config = load_config()
    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    db = Database(config.db_path)
    try:
        report, row_id = generate_and_persist_daily_report(
            db, day, config=config, vision=args.vision
        )
    finally:
        db.close()
    print(report.body_markdown)
    print(f"(persisted as reports.id={row_id}, type=daily, period_key={day.isoformat()})")
    return 0


def cmd_report_weekly(args: argparse.Namespace) -> int:
    _setup_logging()
    config = load_config()
    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()
    db = Database(config.db_path)
    try:
        report, row_id = generate_and_persist_weekly_report(
            db, day, config=config, vision=args.vision
        )
    finally:
        db.close()
    print(report.body_markdown)
    print(f"(persisted as reports.id={row_id}, type=weekly, period_key={report.week_key})")
    return 0


def cmd_report_show(args: argparse.Namespace) -> int:
    if args.type == "weekly" and not args.period:
        print("argus report show --type weekly requires --period YYYY-Www")
        return 1
    if args.type == "daily" and not args.date:
        print("argus report show --type daily requires --date YYYY-MM-DD")
        return 1
    config = load_config()
    db = Database(config.db_path)
    period_key = args.period if args.type == "weekly" else args.date
    try:
        row = db.get_report(args.type, period_key)
    finally:
        db.close()
    if row is None:
        hint = (
            f"argus report weekly --date <date-in-week>"
            if args.type == "weekly"
            else f"argus report daily --date {period_key}"
        )
        print(f"No {args.type} report stored for {period_key}. Run `{hint}` first.")
        return 1
    print(row[5])  # body_markdown
    return 0


def cmd_report_list(args: argparse.Namespace) -> int:
    config = load_config()
    db = Database(config.db_path)
    try:
        rows = db.list_reports()
    finally:
        db.close()
    if not rows:
        print("No reports stored yet.")
        return 0
    print(f"{'type':8s} {'period_key':12s} generated_at")
    for r in rows:
        print(f"{r[1]:8s} {r[2]:12s} {r[3]}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    print("usage: argus report {daily|weekly|show|list} ...")
    return 1


def cmd_purge(args: argparse.Namespace) -> int:
    _setup_logging()
    config = load_config()
    db = Database(config.db_path)
    try:
        result = run_purge(db, config, retention_days=args.days, dry_run=args.dry_run)
    finally:
        db.close()
    print(result.summary())
    if result.removed_paths:
        label = "Would remove" if args.dry_run else "Removed"
        print(f"{label} files:")
        for p in result.removed_paths:
            print(f"  {p}")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    _setup_logging()
    import uvicorn

    from argus.dashboard.app import create_app

    config = load_config()
    port = config.get("dashboard", "port", default=8477)
    app = create_app(config)
    logging.getLogger("argus.cli").info(
        "Starting Argus dashboard on http://127.0.0.1:%d (Ctrl-C to stop)", port
    )
    uvicorn.run(app, host="127.0.0.1", port=port)
    return 0


def cmd_tray(args: argparse.Namespace) -> int:
    _setup_logging()
    from argus.tray import run_tray

    config = load_config()
    run_tray(config)
    return 0


def cmd_service_install(args: argparse.Namespace) -> int:
    _setup_logging()
    return service_mod.install()


def cmd_service_uninstall(args: argparse.Namespace) -> int:
    _setup_logging()
    return service_mod.uninstall()


def cmd_service_status(args: argparse.Namespace) -> int:
    return service_mod.status()


def cmd_service(args: argparse.Namespace) -> int:
    print("usage: argus service {install|uninstall|status}")
    return 1


def _not_implemented(name: str):
    def _cmd(args: argparse.Namespace) -> int:
        print(f"argus {name}: not implemented (later phase)")
        return 1

    return _cmd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="argus", description="Argus — the never-sleeping activity tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run the Argus daemon in the foreground")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="Show platform/backend, config, db path, and row counts")
    p_status.set_defaults(func=cmd_status)

    p_report = sub.add_parser("report", help="Generate/show/list reports (on demand only, no scheduler)")
    p_report.set_defaults(func=cmd_report)
    report_sub = p_report.add_subparsers(dest="report_command")

    p_report_daily = report_sub.add_parser("daily", help="Generate + persist + print the daily report")
    p_report_daily.add_argument(
        "--date", default=None, help="YYYY-MM-DD (local time), defaults to today"
    )
    p_report_daily.add_argument(
        "--vision",
        action="store_true",
        default=False,
        help=(
            "Opt in to on-demand cloud vision narratives: samples a few "
            "screenshots per session and uploads them to the configured "
            "vision API (Gemini by default). Off by default — without "
            "this flag, no screenshots are touched and no network calls "
            "are made. Requires GEMINI_API_KEY or GOOGLE_API_KEY in the "
            "environment."
        ),
    )
    p_report_daily.set_defaults(func=cmd_report_daily)

    p_report_weekly = report_sub.add_parser("weekly", help="Generate + persist + print the weekly report")
    p_report_weekly.add_argument(
        "--date", default=None,
        help="YYYY-MM-DD (local time) of any day in the target ISO week; defaults to today",
    )
    p_report_weekly.add_argument(
        "--vision",
        action="store_true",
        default=False,
        help=(
            "Opt in to on-demand cloud vision narratives for the week: "
            f"samples screenshots from only the week's top {WEEKLY_VISION_TOP_SESSIONS} "
            "longest sessions (not every session/day) and uploads them to "
            "the configured vision API. Off by default. Requires "
            "GEMINI_API_KEY or GOOGLE_API_KEY in the environment."
        ),
    )
    p_report_weekly.set_defaults(func=cmd_report_weekly)

    p_report_show = report_sub.add_parser("show", help="Print a previously persisted daily or weekly report")
    p_report_show.add_argument(
        "--type", choices=["daily", "weekly"], default="daily", help="Report type (default: daily)"
    )
    p_report_show.add_argument("--date", default=None, help="YYYY-MM-DD (for --type daily)")
    p_report_show.add_argument(
        "--period", default=None, help="ISO year-week, e.g. 2026-W27 (for --type weekly)"
    )
    p_report_show.set_defaults(func=cmd_report_show)

    p_report_list = report_sub.add_parser("list", help="List persisted reports")
    p_report_list.set_defaults(func=cmd_report_list)

    p_purge = sub.add_parser(
        "purge", help="Delete raw images (+ their DB rows) older than the retention window"
    )
    p_purge.add_argument(
        "--days", type=int, default=None, help="Override storage.retention_days from config"
    )
    p_purge.add_argument(
        "--dry-run", action="store_true", help="Report what would be deleted without deleting"
    )
    p_purge.set_defaults(func=cmd_purge)

    p_service = sub.add_parser("service", help="Install/uninstall/status for the always-on background service")
    p_service.set_defaults(func=cmd_service)
    service_sub = p_service.add_subparsers(dest="service_command")

    p_service_install = service_sub.add_parser(
        "install", help="Install + enable + start the always-on service (systemd user unit / Windows scheduled task)"
    )
    p_service_install.set_defaults(func=cmd_service_install)

    p_service_uninstall = service_sub.add_parser(
        "uninstall", help="Stop + disable + remove the always-on service"
    )
    p_service_uninstall.set_defaults(func=cmd_service_uninstall)

    p_service_status = service_sub.add_parser(
        "status", help="Show whether the always-on service is installed and running"
    )
    p_service_status.set_defaults(func=cmd_service_status)

    p_dashboard = sub.add_parser(
        "dashboard", help="Start the local read-only web dashboard (127.0.0.1, no auth)"
    )
    p_dashboard.set_defaults(func=cmd_dashboard)

    p_pause = sub.add_parser("pause", help="Pause capture (halts window/screen/camera loops) via IPC")
    p_pause.set_defaults(func=cmd_pause)

    p_resume = sub.add_parser("resume", help="Resume capture via IPC")
    p_resume.set_defaults(func=cmd_resume)

    p_quit = sub.add_parser("quit", help="Ask the running daemon to shut down cleanly via IPC")
    p_quit.set_defaults(func=cmd_quit)

    p_tray = sub.add_parser("tray", help="Run the system tray icon (pause/resume/quit/open dashboard)")
    p_tray.set_defaults(func=cmd_tray)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
