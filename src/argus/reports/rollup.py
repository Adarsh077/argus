"""Time rollup: turn raw ``window_events`` rows into sessions and time
aggregates. Pure DB-read logic — no vision, no cloud calls, no writes.

Session definition (fixed by spec)
-----------------------------------
* A session is a contiguous run of events on the **same app**.
* A gap between consecutive events of **<= 60s** on the same app is absorbed
  into the current session (not treated as a break).
* A gap **> 60s** (regardless of app), OR a switch to a **different app**,
  ends the current session and starts a new one.
* Sessions split on ``app`` only — window *title* changes within the same
  app do NOT start a new session. All distinct titles seen during a
  session are collected into ``Session.titles``.

Duration convention
--------------------
window_events are sampled every ``SAMPLE_INTERVAL_SECONDS`` (5s). For a
given event, we don't know how long that exact state held beyond "at least
until the next sample" so we approximate:

* For any event that is *not* the last event in the whole queried range,
  its contributed duration is ``min(gap_to_next_event, GAP_TOLERANCE_SECONDS)``.
  Capping at the gap tolerance (60s) prevents a single missed sample /
  daemon-paused/sleep gap from being counted as active time.
* The very last event in the range is assumed to have lasted one more
  sample interval (``SAMPLE_INTERVAL_SECONDS``, 5s) — we have no
  information about what happened after it.

This same per-event duration is used consistently for:
  - total tracked time
  - time-per-app
  - time-per-window (per (app, title) pair)
  - session start/end/duration

Micro-sessions
--------------
Sessions whose total duration is **< MICRO_SESSION_SECONDS (30s)** are
**dropped** from the session/timeline list (not merged into a neighbor).
Rationale: merging a sub-30s blip on app X into a neighboring session on
app Y would misattribute time or titles to the wrong app, and a pure
"absorb into whichever side is same app" rule rarely applies (by
definition a micro-session already differs in app from at least one
neighbor, that's why it's an isolated short session). Dropping keeps the
timeline clean and consistent. NOTE: dropping a session from the timeline
does NOT drop its time from the total/per-app/per-window aggregates —
those are computed independently from the raw per-event durations, so the
overall accounting stays fully accurate; only the *session timeline* is
decluttered.

Day boundaries
--------------
Events are stored as UTC. A "daily" report is a calendar day in the
**machine's local timezone** (more useful to a human than a UTC-day
cutoff). See :func:`local_day_range_utc`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from argus.db import Database

SAMPLE_INTERVAL_SECONDS = 5.0
GAP_TOLERANCE_SECONDS = 60.0
MICRO_SESSION_SECONDS = 30.0


@dataclass
class WindowEvent:
    ts: datetime
    app: str
    window_title: str | None
    monitor: str | None


@dataclass
class Session:
    app: str
    start: datetime
    end: datetime
    titles: set[str] = field(default_factory=set)

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class RollupResult:
    range_start: datetime
    range_end: datetime
    total_seconds: float
    per_app_seconds: dict[str, float]
    per_window_seconds: dict[tuple[str, str], float]
    sessions: list[Session]
    event_count: int


def local_day_range_utc(day: date) -> tuple[datetime, datetime]:
    """Return the [start, end) UTC instants covering ``day`` as a calendar
    day in the machine's local timezone.

    Uses the current local UTC offset (via ``datetime.now().astimezone()``)
    as a fixed-offset tzinfo. This is a simplification: it does not
    correctly handle a DST transition that happens to fall on the queried
    day. Acceptable for this backbone; can be revisited with ``zoneinfo``
    if that ever matters in practice.
    """
    local_tz = datetime.now().astimezone().tzinfo
    start_local = datetime(day.year, day.month, day.day, tzinfo=local_tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_window_events(db: Database, start_utc: datetime, end_utc: datetime) -> list[WindowEvent]:
    """Read window_events with ts in [start_utc, end_utc), ordered by ts."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT ts, app, window_title, monitor FROM window_events "
            "WHERE ts >= ? AND ts < ? ORDER BY ts ASC",
            (start_utc.isoformat(), end_utc.isoformat()),
        )
        rows = cur.fetchall()
    return [
        WindowEvent(ts=_parse_ts(row[0]), app=row[1], window_title=row[2], monitor=row[3])
        for row in rows
    ]


def _event_durations(events: list[WindowEvent]) -> list[float]:
    """Per-event duration in seconds, per the module-level convention."""
    durations: list[float] = []
    n = len(events)
    for i, ev in enumerate(events):
        if i + 1 < n:
            gap = (events[i + 1].ts - ev.ts).total_seconds()
            durations.append(min(max(gap, 0.0), GAP_TOLERANCE_SECONDS))
        else:
            durations.append(SAMPLE_INTERVAL_SECONDS)
    return durations


def _build_sessions(events: list[WindowEvent], durations: list[float]) -> list[Session]:
    sessions: list[Session] = []
    if not events:
        return sessions

    cur_app = events[0].app
    cur_start = events[0].ts
    cur_titles: set[str] = set()
    if events[0].window_title:
        cur_titles.add(events[0].window_title)
    cur_end = events[0].ts + timedelta(seconds=durations[0])

    for i in range(1, len(events)):
        ev = events[i]
        gap = (ev.ts - events[i - 1].ts).total_seconds()
        same_app = ev.app == cur_app
        within_gap = gap <= GAP_TOLERANCE_SECONDS
        if same_app and within_gap:
            # Extend current session.
            cur_end = ev.ts + timedelta(seconds=durations[i])
            if ev.window_title:
                cur_titles.add(ev.window_title)
        else:
            sessions.append(Session(app=cur_app, start=cur_start, end=cur_end, titles=cur_titles))
            cur_app = ev.app
            cur_start = ev.ts
            cur_titles = set()
            if ev.window_title:
                cur_titles.add(ev.window_title)
            cur_end = ev.ts + timedelta(seconds=durations[i])

    sessions.append(Session(app=cur_app, start=cur_start, end=cur_end, titles=cur_titles))

    # Drop micro-sessions (documented in module docstring).
    sessions = [s for s in sessions if s.duration_seconds >= MICRO_SESSION_SECONDS]
    return sessions


def compute_rollup(db: Database, start_utc: datetime, end_utc: datetime) -> RollupResult:
    """Compute the full time rollup for events in [start_utc, end_utc)."""
    events = fetch_window_events(db, start_utc, end_utc)
    durations = _event_durations(events)

    per_app: dict[str, float] = {}
    per_window: dict[tuple[str, str], float] = {}
    for ev, dur in zip(events, durations):
        per_app[ev.app] = per_app.get(ev.app, 0.0) + dur
        title = ev.window_title or ""
        key = (ev.app, title)
        per_window[key] = per_window.get(key, 0.0) + dur

    sessions = _build_sessions(events, durations)
    total_seconds = sum(durations)

    return RollupResult(
        range_start=start_utc,
        range_end=end_utc,
        total_seconds=total_seconds,
        per_app_seconds=per_app,
        per_window_seconds=per_window,
        sessions=sessions,
        event_count=len(events),
    )


def compute_daily_rollup(db: Database, day: date) -> RollupResult:
    start_utc, end_utc = local_day_range_utc(day)
    return compute_rollup(db, start_utc, end_utc)
