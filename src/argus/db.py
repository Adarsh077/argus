"""SQLite schema + access for Argus.

All timestamps are stored as timezone-aware UTC ISO-8601 strings.
WAL mode is enabled for concurrent readers/writers (dashboard reads while
the daemon writes, in later phases).
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS window_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    app TEXT NOT NULL,
    window_title TEXT,
    monitor TEXT
);

CREATE TABLE IF NOT EXISTS screenshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    path TEXT NOT NULL,
    monitors TEXT
);

CREATE TABLE IF NOT EXISTS camera_frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    path TEXT NOT NULL,
    device TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    period_key TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    data_json TEXT NOT NULL,
    body_markdown TEXT NOT NULL,
    UNIQUE (type, period_key)
);

CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,           -- recording START, UTC ISO-8601 (also the retention cutoff key)
    ts_end TEXT,               -- recording END, UTC ISO-8601 (null only if a crash left it unfinalized)
    path TEXT NOT NULL,
    monitors TEXT,
    duration_seconds REAL
);

CREATE INDEX IF NOT EXISTS idx_window_events_ts ON window_events (ts);
CREATE INDEX IF NOT EXISTS idx_screenshots_ts ON screenshots (ts);
CREATE INDEX IF NOT EXISTS idx_camera_frames_ts ON camera_frames (ts);
CREATE INDEX IF NOT EXISTS idx_reports_type_period ON reports (type, period_key);
CREATE INDEX IF NOT EXISTS idx_recordings_ts ON recordings (ts);
"""

TABLES = ("window_events", "screenshots", "camera_frames", "reports", "recordings")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    def insert_window_event(self, app: str, window_title: str, monitor: str, ts: str | None = None) -> int:
        ts = ts or utc_now_iso()
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO window_events (ts, app, window_title, monitor) VALUES (?, ?, ?, ?)",
                (ts, app, window_title, monitor),
            )
            return cur.lastrowid

    def insert_screenshot(self, path: str, monitors: str, ts: str | None = None) -> int:
        ts = ts or utc_now_iso()
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO screenshots (ts, path, monitors) VALUES (?, ?, ?)",
                (ts, path, monitors),
            )
            return cur.lastrowid

    def insert_camera_frame(self, path: str, device: str, ts: str | None = None) -> int:
        ts = ts or utc_now_iso()
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO camera_frames (ts, path, device) VALUES (?, ?, ?)",
                (ts, path, device),
            )
            return cur.lastrowid

    def insert_recording(
        self,
        path: str,
        monitors: str,
        ts_start: str,
        ts_end: str,
        duration_seconds: float,
    ) -> int:
        """Record a finished screen recording. Inserted once, on stop, after
        the mp4 is finalized on disk (so a row always points at a playable
        file)."""
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO recordings (ts, ts_end, path, monitors, duration_seconds) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts_start, ts_end, path, monitors, duration_seconds),
            )
            return cur.lastrowid

    def list_recordings_between(self, start_utc_iso: str, end_utc_iso: str) -> list[sqlite3.Row]:
        """Read-only: recordings whose start ts is in [start, end), newest
        first. Used by the dashboard recordings page."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT id, ts, ts_end, path, monitors, duration_seconds FROM recordings "
                "WHERE ts >= ? AND ts < ? ORDER BY ts DESC",
                (start_utc_iso, end_utc_iso),
            )
            return cur.fetchall()

    def list_recordings(self) -> list[sqlite3.Row]:
        """Read-only: all recordings, newest first."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT id, ts, ts_end, path, monitors, duration_seconds FROM recordings "
                "ORDER BY ts DESC"
            )
            return cur.fetchall()

    def upsert_report(
        self,
        report_type: str,
        period_key: str,
        data_json: str,
        body_markdown: str,
        generated_at: str | None = None,
    ) -> int:
        """Insert or replace a report for (type, period_key).

        Regenerating a report for the same period REPLACES the previous row
        in place (same `id` is not preserved, but there is only ever one
        row per (type, period_key) — no history of past regenerations is
        kept). This keeps the reports table simple; if versioning is ever
        needed it can be layered on later.
        """
        generated_at = generated_at or utc_now_iso()
        with self.cursor() as cur:
            cur.execute(
                """
                INSERT INTO reports (type, period_key, generated_at, data_json, body_markdown)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (type, period_key) DO UPDATE SET
                    generated_at = excluded.generated_at,
                    data_json = excluded.data_json,
                    body_markdown = excluded.body_markdown
                """,
                (report_type, period_key, generated_at, data_json, body_markdown),
            )
            cur.execute(
                "SELECT id FROM reports WHERE type = ? AND period_key = ?",
                (report_type, period_key),
            )
            return cur.fetchone()[0]

    def get_report(self, report_type: str, period_key: str) -> sqlite3.Row | None:
        with self.cursor() as cur:
            cur.execute(
                "SELECT id, type, period_key, generated_at, data_json, body_markdown "
                "FROM reports WHERE type = ? AND period_key = ?",
                (report_type, period_key),
            )
            return cur.fetchone()

    def list_reports(self, report_type: str | None = None) -> list[sqlite3.Row]:
        with self.cursor() as cur:
            if report_type:
                cur.execute(
                    "SELECT id, type, period_key, generated_at FROM reports "
                    "WHERE type = ? ORDER BY period_key DESC",
                    (report_type,),
                )
            else:
                cur.execute(
                    "SELECT id, type, period_key, generated_at FROM reports ORDER BY period_key DESC"
                )
            return cur.fetchall()

    def list_screenshots_between(self, start_utc_iso: str, end_utc_iso: str) -> list[sqlite3.Row]:
        """Read-only: screenshots with ts in [start, end), ordered by ts.
        Used by the dashboard to browse captures for a given day."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT id, ts, path, monitors FROM screenshots "
                "WHERE ts >= ? AND ts < ? ORDER BY ts DESC",
                (start_utc_iso, end_utc_iso),
            )
            return cur.fetchall()

    def list_camera_frames_between(self, start_utc_iso: str, end_utc_iso: str) -> list[sqlite3.Row]:
        """Read-only: camera frames with ts in [start, end), ordered by ts.
        Used by the dashboard to browse captures for a given day."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT id, ts, path, device FROM camera_frames "
                "WHERE ts >= ? AND ts < ? ORDER BY ts DESC",
                (start_utc_iso, end_utc_iso),
            )
            return cur.fetchall()

    def nearest_window_event(self, ts_iso: str) -> sqlite3.Row | None:
        """Best-effort read-only lookup: the window_events row whose ts is
        closest to ``ts_iso`` (checked on both sides). Used by the
        dashboard to label captures with the app/window title that was
        likely active at capture time. Returns None if there are no
        window_events at all."""
        with self.cursor() as cur:
            cur.execute(
                "SELECT ts, app, window_title, monitor FROM window_events "
                "WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
                (ts_iso,),
            )
            before = cur.fetchone()
            cur.execute(
                "SELECT ts, app, window_title, monitor FROM window_events "
                "WHERE ts > ? ORDER BY ts ASC LIMIT 1",
                (ts_iso,),
            )
            after = cur.fetchone()
        if before is None:
            return after
        if after is None:
            return before
        # Pick whichever is closer in time.
        t0 = datetime.fromisoformat(ts_iso)
        tb = datetime.fromisoformat(before[0])
        ta = datetime.fromisoformat(after[0])
        return before if abs((t0 - tb).total_seconds()) <= abs((ta - t0).total_seconds()) else after

    def row_counts(self) -> dict[str, int]:
        counts = {}
        with self.cursor() as cur:
            for table in TABLES:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                counts[table] = cur.fetchone()[0]
        return counts

    def close(self) -> None:
        self._conn.close()
