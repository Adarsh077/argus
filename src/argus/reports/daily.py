"""Daily report generation + persistence.

Vision narratives (Phase 7)
----------------------------
By default (``vision=False``), each session is rendered with a placeholder
line (``narrative: [not yet generated — Phase 7]``) and no network calls
are made — behavior identical to Phase 5/6.

Pass ``vision=True`` (with a config) to opt in: for each session we sample
a handful of representative screenshots (``argus.vision.sampler``) and call
a cloud vision API (``argus.vision.client``) to get a short narrative,
which is slotted into both the JSON ``session["narrative"]`` field and the
rendered markdown. If no API key is present in the environment, sessions
get a ``narrative unavailable: no API key`` note instead, and generation
still completes fully (rollup etc.) — never crashes.

This is on-demand only, triggered by the ``--vision`` CLI flag — never
called from the background daemon or a scheduler.

Reports are on-demand only — this module has no scheduler; generation
happens exactly when ``generate_daily_report`` is called (i.e. from the
CLI). Regenerating a report for a date REPLACES the previously stored row
for that date (see ``Database.upsert_report``).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime

from argus.config import Config
from argus.db import Database, utc_now_iso
from argus.reports.rollup import RollupResult, Session, compute_daily_rollup
from argus.vision.client import generate_session_narrative
from argus.vision.sampler import sample_session_screenshots

REPORT_TYPE_DAILY = "daily"

logger = logging.getLogger("argus.reports.daily")

NARRATIVE_PLACEHOLDER = "[not yet generated — Phase 7]"
NARRATIVE_NO_KEY = "narrative unavailable: no API key"


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
    return dt.astimezone().strftime("%H:%M:%S")


def _rollup_to_dict(rollup: RollupResult, narratives: list[str | None] | None = None) -> dict:
    narratives = narratives or [None] * len(rollup.sessions)
    return {
        "range_start_utc": rollup.range_start.isoformat(),
        "range_end_utc": rollup.range_end.isoformat(),
        "event_count": rollup.event_count,
        "total_seconds": rollup.total_seconds,
        "per_app_seconds": rollup.per_app_seconds,
        "per_window_seconds": {
            f"{app}\x1f{title}": secs for (app, title), secs in rollup.per_window_seconds.items()
        },
        "sessions": [
            {
                "app": s.app,
                "start_utc": s.start.isoformat(),
                "end_utc": s.end.isoformat(),
                "duration_seconds": s.duration_seconds,
                "titles": sorted(s.titles),
                "narrative": narrative,
            }
            for s, narrative in zip(rollup.sessions, narratives)
        ],
    }


def _narrative_line(narrative: str | None) -> str:
    if narrative is None:
        return f"  - narrative: _{NARRATIVE_PLACEHOLDER}_"
    if narrative == NARRATIVE_NO_KEY:
        return f"  - narrative: _{NARRATIVE_NO_KEY}_"
    return f"  - narrative: {narrative}"


def _generate_narratives(
    db: Database, config: Config, sessions: list[Session]
) -> tuple[list[str | None], int]:
    """Sample screenshots + call the vision API for each session.

    Returns (narratives, images_uploaded). Narratives are aligned 1:1 with
    ``sessions``. If no API key is present in the environment, every entry
    is set to ``NARRATIVE_NO_KEY`` and a single top-level warning is logged
    (not per-session) — no network calls are attempted in that case.
    """
    api_key = config.api_key()
    if api_key is None:
        logger.warning(
            "Vision narratives requested (--vision) but no API key found in "
            "environment (checked GEMINI_API_KEY, GOOGLE_API_KEY); skipping "
            "narrative generation for all sessions."
        )
        return [NARRATIVE_NO_KEY] * len(sessions), 0

    endpoint = config.get("vision", "endpoint")
    model = config.get("vision", "model")
    sampling_count = config.get("vision", "sampling_count", default=3)

    narratives: list[str | None] = []
    images_uploaded = 0
    for s in sessions:
        image_paths = sample_session_screenshots(db, s.start, s.end, sampling_count)
        if not image_paths:
            narratives.append(None)
            continue
        narrative = generate_session_narrative(
            api_key=api_key,
            app=s.app,
            titles=sorted(s.titles),
            image_paths=image_paths,
            endpoint=endpoint,
            model=model,
        )
        narratives.append(narrative)
        images_uploaded += len(image_paths)

    return narratives, images_uploaded


def _render_markdown(day: date, rollup: RollupResult, narratives: list[str | None] | None = None) -> str:
    narratives = narratives or [None] * len(rollup.sessions)
    lines: list[str] = []
    lines.append(f"# Daily report — {day.isoformat()}")
    lines.append("")
    lines.append(f"Total tracked time: **{_fmt_duration(rollup.total_seconds)}** "
                 f"({rollup.event_count} window-event samples)")
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

    lines.append("## Session timeline")
    lines.append(
        "_(sessions are contiguous same-app runs; gaps up to 60s are absorbed; "
        "sessions under 30s are dropped from this timeline)_"
    )
    if not rollup.sessions:
        lines.append("- (no sessions)")
    for s, narrative in zip(rollup.sessions, narratives):
        titles = ", ".join(sorted(s.titles)) if s.titles else "(no title)"
        lines.append(
            f"- {_fmt_local(s.start)}–{_fmt_local(s.end)} "
            f"({_fmt_duration(s.duration_seconds)}) **{s.app}** — {titles}"
        )
        lines.append(_narrative_line(narrative))
    lines.append("")
    return "\n".join(lines)


@dataclass
class DailyReport:
    day: date
    rollup: RollupResult
    body_markdown: str
    data: dict

    def data_json(self) -> str:
        return json.dumps(self.data)


def generate_daily_report(
    db: Database, day: date, config: Config | None = None, vision: bool = False
) -> DailyReport:
    """Run the rollup for ``day`` (local calendar day) and render a report.

    Does not persist — call :func:`persist_daily_report` separately (the
    CLI does both).

    ``vision=False`` (the default) is a no-op with respect to Phase 7: no
    screenshots are sampled, no network calls are made, and the narrative
    fields stay as the "not yet generated" placeholder — behavior is
    unchanged from Phase 5/6. Pass ``vision=True`` (with a ``config``) to
    opt in to on-demand vision narrative generation; this is only ever
    triggered by an explicit CLI flag (``argus report daily --vision``),
    never from the background daemon.
    """
    rollup = compute_daily_rollup(db, day)

    narratives: list[str | None] | None = None
    images_uploaded = 0
    if vision:
        if config is None:
            raise ValueError("generate_daily_report(vision=True) requires a config")
        narratives, images_uploaded = _generate_narratives(db, config, rollup.sessions)
        logger.info(
            "Uploaded %d images across %d sessions for vision narratives.",
            images_uploaded,
            len(rollup.sessions),
        )

    data = _rollup_to_dict(rollup, narratives)
    body = _render_markdown(day, rollup, narratives)
    return DailyReport(day=day, rollup=rollup, body_markdown=body, data=data)


def persist_daily_report(db: Database, report: DailyReport) -> int:
    """Persist (insert or replace) the report for its date. Returns row id."""
    return db.upsert_report(
        report_type=REPORT_TYPE_DAILY,
        period_key=report.day.isoformat(),
        data_json=report.data_json(),
        body_markdown=report.body_markdown,
        generated_at=utc_now_iso(),
    )


def generate_and_persist_daily_report(
    db: Database, day: date, config: Config | None = None, vision: bool = False
) -> tuple[DailyReport, int]:
    report = generate_daily_report(db, day, config=config, vision=vision)
    row_id = persist_daily_report(db, report)
    return report, row_id
