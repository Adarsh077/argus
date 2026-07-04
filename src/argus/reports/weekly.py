"""Weekly report generation + persistence.

Week boundaries
---------------
A "weekly" report covers an **ISO week (Monday–Sunday)**, using **local
calendar day** boundaries — consistent with ``reports/daily.py`` and
``rollup.local_day_range_utc``. Concretely: given a date, we find the
Monday of its ISO week (``date - timedelta(days=date.isoweekday() - 1)``)
and the following Sunday, then compute the rollup range as
``[local_day_range_utc(monday)[0], local_day_range_utc(sunday)[1])`` — i.e.
the local midnight that starts Monday through the local midnight that ends
Sunday. This reuses ``rollup.compute_rollup`` over that whole 7-day range;
no new rollup logic is needed.

The report is keyed by ISO year-week, e.g. ``2026-W27`` (``date.isocalendar()``).
Note the ISO year of a date can differ from its calendar year near year
boundaries — we use ``isocalendar()`` for both the week number and the year
component of the key, which is the standard/expected behavior.

Narratives for a week: aggregate, don't re-narrate
---------------------------------------------------
Re-running vision narration for every session across 7 days would be both
expensive (many more images) and noisy (a wall of per-session narratives
duplicating what's already in the daily reports). Instead:

* **Default (no ``--vision``)**: the weekly report is a pure metadata
  rollup (total time, per-app, per-window, per-day breakdown, session
  count) PLUS a "daily narratives" section that pulls any already-persisted
  ``daily`` report rows (type='daily') for the days in this week and quotes
  their per-session narratives verbatim (only the narrative lines, not the
  full markdown). If a day has no persisted daily report (most days, most
  of the time), it's simply omitted from that section — no new narration
  or vision calls are triggered. No network calls happen in this path.

* **``--vision`` opt-in**: in addition to the above, we sample a SMALL,
  bounded number of screenshots across the week's longest sessions (not
  every session, not every day) and generate a handful of high-level
  narratives for that week. Specifically: take the top
  ``WEEKLY_VISION_TOP_SESSIONS`` (default 5) longest sessions across the
  whole week (by duration), and run the same per-session narrative
  pipeline used by the daily report (``vision.sampler`` +
  ``vision.client``) on just those. This keeps the upload count small and
  bounded regardless of how many sessions occurred during the week. The
  number of images uploaded is logged, same convention as daily.

Persistence
-----------
Stored in the same ``reports`` table as daily reports, with
``type='weekly'`` and ``period_key`` like ``'2026-W27'``. Regenerating a
week's report replaces the row in place (same upsert convention as daily,
via ``Database.upsert_report``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from argus.config import Config
from argus.db import Database, utc_now_iso
from argus.reports.daily import (
    NARRATIVE_NO_KEY,
    _generate_narratives,
    _narrative_line,
)
from argus.reports.rollup import RollupResult, Session, compute_rollup, local_day_range_utc

REPORT_TYPE_WEEKLY = "weekly"
REPORT_TYPE_DAILY = "daily"

WEEKLY_VISION_TOP_SESSIONS = 5

logger = logging.getLogger("argus.reports.weekly")


def _fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def iso_week_key(day: date) -> str:
    year, week, _ = day.isocalendar()
    return f"{year:04d}-W{week:02d}"


def week_monday_sunday(day: date) -> tuple[date, date]:
    """Return (monday, sunday) dates of the ISO week containing ``day``."""
    monday = day - timedelta(days=day.isoweekday() - 1)
    sunday = monday + timedelta(days=6)
    return monday, sunday


def week_range_utc(day: date) -> tuple[datetime, datetime, date, date]:
    """Return (start_utc, end_utc, monday, sunday) for the ISO week
    containing ``day``, using local-day boundaries (see module docstring).
    """
    monday, sunday = week_monday_sunday(day)
    start_utc, _ = local_day_range_utc(monday)
    _, end_utc = local_day_range_utc(sunday)
    return start_utc, end_utc, monday, sunday


def _per_day_breakdown(db: Database, monday: date) -> list[dict]:
    """Per-day totals (total_seconds, event_count, session_count) for each
    of the 7 local calendar days in the week, computed via independent
    daily rollups (reusing ``compute_rollup``/``local_day_range_utc``).
    """
    days = []
    for i in range(7):
        day = monday + timedelta(days=i)
        start_utc, end_utc = local_day_range_utc(day)
        day_rollup = compute_rollup(db, start_utc, end_utc)
        days.append(
            {
                "date": day.isoformat(),
                "total_seconds": day_rollup.total_seconds,
                "event_count": day_rollup.event_count,
                "session_count": len(day_rollup.sessions),
            }
        )
    return days


def _collect_daily_narratives(db: Database, monday: date, sunday: date) -> list[dict]:
    """Pull already-persisted daily report narratives for days in this
    week, if any exist. Does not generate anything new; pure DB read.
    """
    found = []
    day = monday
    while day <= sunday:
        row = db.get_report(REPORT_TYPE_DAILY, day.isoformat())
        if row is not None:
            try:
                data = json.loads(row[4])  # data_json
            except (TypeError, ValueError, json.JSONDecodeError):
                data = {}
            sessions = data.get("sessions", [])
            narratives = [
                {"app": s.get("app"), "narrative": s.get("narrative")}
                for s in sessions
                if s.get("narrative") not in (None, NARRATIVE_NO_KEY)
            ]
            if narratives:
                found.append({"date": day.isoformat(), "narratives": narratives})
        day += timedelta(days=1)
    return found


def _rollup_to_dict(
    rollup: RollupResult,
    per_day: list[dict],
    daily_narratives: list[dict],
    top_session_narratives: list[dict] | None = None,
) -> dict:
    return {
        "range_start_utc": rollup.range_start.isoformat(),
        "range_end_utc": rollup.range_end.isoformat(),
        "event_count": rollup.event_count,
        "total_seconds": rollup.total_seconds,
        "session_count": len(rollup.sessions),
        "per_app_seconds": rollup.per_app_seconds,
        "per_window_seconds": {
            f"{app}\x1f{title}": secs for (app, title), secs in rollup.per_window_seconds.items()
        },
        "per_day": per_day,
        "daily_narratives": daily_narratives,
        "top_session_narratives": top_session_narratives or [],
    }


def _render_markdown(
    week_key: str,
    monday: date,
    sunday: date,
    rollup: RollupResult,
    per_day: list[dict],
    daily_narratives: list[dict],
    top_session_narratives: list[dict] | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"# Weekly report — {week_key} ({monday.isoformat()} to {sunday.isoformat()})")
    lines.append("")
    lines.append(
        f"Total tracked time: **{_fmt_duration(rollup.total_seconds)}** "
        f"({rollup.event_count} window-event samples, {len(rollup.sessions)} sessions)"
    )
    lines.append("")

    lines.append("## Time per app")
    for app, secs in sorted(rollup.per_app_seconds.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"- {app}: {_fmt_duration(secs)}")
    if not rollup.per_app_seconds:
        lines.append("- (no activity recorded)")
    lines.append("")

    lines.append("## Top windows")
    top_windows = sorted(rollup.per_window_seconds.items(), key=lambda kv: kv[1], reverse=True)[:10]
    for (app, title), secs in top_windows:
        label = f"{app} — {title}" if title else app
        lines.append(f"- {label}: {_fmt_duration(secs)}")
    if not top_windows:
        lines.append("- (no activity recorded)")
    lines.append("")

    lines.append("## Per-day breakdown")
    for d in per_day:
        lines.append(
            f"- {d['date']}: {_fmt_duration(d['total_seconds'])} "
            f"({d['session_count']} sessions, {d['event_count']} samples)"
        )
    lines.append("")

    lines.append("## Daily narratives (from persisted daily reports, if any)")
    if not daily_narratives:
        lines.append(
            "_(no daily reports with vision narratives found for this week — "
            "run `argus report daily --date YYYY-MM-DD --vision` for a day "
            "first, or pass `--vision` to this weekly report for a small "
            "top-session sample)_"
        )
    for entry in daily_narratives:
        lines.append(f"### {entry['date']}")
        for n in entry["narratives"]:
            lines.append(f"- **{n['app']}**: {n['narrative']}")
    lines.append("")

    lines.append("## Top-session narratives (--vision, this week's longest sessions)")
    if not top_session_narratives:
        lines.append("_(none — pass `--vision` to sample the week's longest sessions)_")
    for entry in top_session_narratives:
        s = entry["session"]
        lines.append(
            f"- {s['start_utc']}–{s['end_utc']} ({_fmt_duration(s['duration_seconds'])}) "
            f"**{s['app']}**"
        )
        lines.append(_narrative_line(entry["narrative"]))
    lines.append("")

    return "\n".join(lines)


@dataclass
class WeeklyReport:
    week_key: str
    monday: date
    sunday: date
    rollup: RollupResult
    body_markdown: str
    data: dict

    def data_json(self) -> str:
        return json.dumps(self.data)


def _generate_top_session_narratives(
    db: Database, config: Config, sessions: list[Session]
) -> tuple[list[dict], int]:
    """Vision narratives for the top N longest sessions of the week only.

    Reuses ``daily._generate_narratives`` (same sampler + client pipeline)
    but restricts the input to a small, bounded subset of sessions so a
    week never uploads more than ``WEEKLY_VISION_TOP_SESSIONS`` sessions'
    worth of images, regardless of how many sessions occurred.
    """
    top_sessions = sorted(sessions, key=lambda s: s.duration_seconds, reverse=True)[
        :WEEKLY_VISION_TOP_SESSIONS
    ]
    narratives, images_uploaded = _generate_narratives(db, config, top_sessions)
    entries = [
        {
            "session": {
                "app": s.app,
                "start_utc": s.start.isoformat(),
                "end_utc": s.end.isoformat(),
                "duration_seconds": s.duration_seconds,
            },
            "narrative": narrative,
        }
        for s, narrative in zip(top_sessions, narratives)
    ]
    return entries, images_uploaded


def generate_weekly_report(
    db: Database, day: date, config: Config | None = None, vision: bool = False
) -> WeeklyReport:
    """Generate (but do not persist) the weekly report for the ISO week
    containing ``day``.

    ``vision=False`` (default): pure metadata rollup over the 7-day range,
    plus any already-persisted daily-report narratives for days in this
    week. No screenshots are touched, no network calls are made.

    ``vision=True``: additionally samples + narrates a small, bounded
    number of the week's longest sessions (see
    ``WEEKLY_VISION_TOP_SESSIONS``). Requires ``config``.
    """
    start_utc, end_utc, monday, sunday = week_range_utc(day)
    rollup = compute_rollup(db, start_utc, end_utc)
    per_day = _per_day_breakdown(db, monday)
    daily_narratives = _collect_daily_narratives(db, monday, sunday)

    top_session_narratives: list[dict] = []
    if vision:
        if config is None:
            raise ValueError("generate_weekly_report(vision=True) requires a config")
        top_session_narratives, images_uploaded = _generate_top_session_narratives(
            db, config, rollup.sessions
        )
        logger.info(
            "Uploaded %d images across %d top sessions for weekly vision narratives.",
            images_uploaded,
            len(top_session_narratives),
        )

    week_key = iso_week_key(day)
    data = _rollup_to_dict(rollup, per_day, daily_narratives, top_session_narratives)
    body = _render_markdown(
        week_key, monday, sunday, rollup, per_day, daily_narratives, top_session_narratives
    )
    return WeeklyReport(
        week_key=week_key, monday=monday, sunday=sunday, rollup=rollup, body_markdown=body, data=data
    )


def persist_weekly_report(db: Database, report: WeeklyReport) -> int:
    """Persist (insert or replace) the report for its ISO week. Returns row id."""
    return db.upsert_report(
        report_type=REPORT_TYPE_WEEKLY,
        period_key=report.week_key,
        data_json=report.data_json(),
        body_markdown=report.body_markdown,
        generated_at=utc_now_iso(),
    )


def generate_and_persist_weekly_report(
    db: Database, day: date, config: Config | None = None, vision: bool = False
) -> tuple[WeeklyReport, int]:
    report = generate_weekly_report(db, day, config=config, vision=vision)
    row_id = persist_weekly_report(db, report)
    return report, row_id
