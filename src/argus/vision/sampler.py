"""Pick a small, representative set of screenshots for a session.

Strategy
--------
We do NOT send every screenshot captured during a session to the cloud —
only up to ``sampling_count`` (config: vision.sampling_count, default 3),
evenly spaced across the session's duration, and always including one near
the temporal midpoint.

Concretely: query all ``screenshots`` rows with ``ts`` in
``[session.start, session.end]`` ordered by ts. If there are more than
``sampling_count``, pick indices evenly spaced across the list (including
index 0, the last index, and — for counts >= 3 — an index near the middle);
for small counts this reduces to just taking the first/last/middle
available shots. If there are fewer than or equal to ``sampling_count``
screenshots in range, we use all of them.

Zero-screenshot sessions
-------------------------
Short sessions (under the ~5-minute screenshot tick) can have *zero*
screenshots strictly within [start, end]. Chosen approach (documented here
per the task): fall back to the single nearest screenshot within a small
window *outside* the session bounds (``FALLBACK_WINDOW_SECONDS`` on either
side), preferring whichever is closer in time to the session's midpoint.
This keeps very short sessions from silently having no narrative at all,
while still representing "roughly what was on screen" during that time. If
no screenshot exists even in that widened window, we return an empty list
and the caller skips narrative generation for that session (no crash, no
fabricated data).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argus.db import Database

FALLBACK_WINDOW_SECONDS = 600  # 10 minutes on either side of the session


@dataclass
class ScreenshotRow:
    ts: datetime
    path: str


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fetch_screenshots(db: Database, start: datetime, end: datetime) -> list[ScreenshotRow]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT ts, path FROM screenshots WHERE ts >= ? AND ts <= ? ORDER BY ts ASC",
            (start.isoformat(), end.isoformat()),
        )
        rows = cur.fetchall()
    return [ScreenshotRow(ts=_parse_ts(r[0]), path=r[1]) for r in rows]


def _evenly_spaced_indices(n: int, count: int) -> list[int]:
    """Pick up to ``count`` indices out of ``range(n)``, evenly spaced,
    always including 0, n-1, and a middle index (for count >= 3)."""
    if n <= count:
        return list(range(n))
    if count <= 1:
        return [n // 2]
    # Evenly spaced across [0, n-1], inclusive of both ends.
    step = (n - 1) / (count - 1)
    indices = sorted({round(i * step) for i in range(count)})
    # Rounding collisions can shrink the set below `count`; that's fine —
    # it just means we return slightly fewer than requested.
    return indices


def sample_session_screenshots(
    db: Database,
    session_start: datetime,
    session_end: datetime,
    sampling_count: int = 3,
) -> list[str]:
    """Return up to ``sampling_count`` screenshot file paths representative
    of the session window, ordered by time. See module docstring for the
    selection strategy and the zero-screenshot fallback.
    """
    rows = _fetch_screenshots(db, session_start, session_end)

    if not rows:
        # Fallback: widen the window and take the single nearest shot to
        # the session midpoint (see module docstring).
        midpoint = session_start + (session_end - session_start) / 2
        widened = _fetch_screenshots(
            db,
            session_start - timedelta(seconds=FALLBACK_WINDOW_SECONDS),
            session_end + timedelta(seconds=FALLBACK_WINDOW_SECONDS),
        )
        if not widened:
            return []
        nearest = min(widened, key=lambda r: abs((r.ts - midpoint).total_seconds()))
        return [nearest.path]

    indices = _evenly_spaced_indices(len(rows), max(1, sampling_count))
    return [rows[i].path for i in indices]
