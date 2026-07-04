"""Retention purge: raw images (screenshots + camera frames) are deleted
after ``storage.retention_days`` (default 30). Narratives, summaries and
reports are kept forever and are never touched here.

What gets purged
-----------------
- Screenshot/camera WebP files under
  ``<data>/images/{screenshots,camera}/YYYY-MM-DD/*.webp``
- The matching rows in the ``screenshots`` / ``camera_frames`` tables.

What is NEVER purged
---------------------
- ``reports`` table (daily/weekly narratives + summaries) — kept forever
  per spec, regardless of age.
- ``window_events`` — deliberately kept forever. The spec only calls out
  "raw images" for the 30-day purge and "narratives and summaries" for
  forever-retention; window_events are neither. They are the small,
  cheap, foundational time-tracking backbone that daily/weekly rollups
  are computed from (see reports/rollup.py) — purging them would silently
  corrupt/blank out historical reports that reference date ranges older
  than the retention window. Default choice: keep them indefinitely.

Cutoff convention
------------------
Screenshot/camera day-folders are named with the UTC calendar day of the
capture timestamp (see capture/screen.py, capture/camera.py — both use
``datetime.now(timezone.utc)`` to build both the row's ``ts`` and the
``YYYY-MM-DD`` folder name). So for purge purposes the cutoff is also
computed in UTC, matching the folder-naming convention exactly:

    cutoff_instant = now(UTC) - retention_days
    cutoff_date    = cutoff_instant.date()

Day-folders strictly older than ``cutoff_date`` are wholly older than the
cutoff and can be removed by row-controlled deletion, and finally by
deleting the (now provably empty, or safely wiped) directory. Today's and
recent folders (>= cutoff_date) are never bulk-removed; even the boundary
day is only touched on a per-row basis so a handful of old rows within an
otherwise-current day cannot cause the whole day to disappear.

Consistency & crash-safety
---------------------------
The DB is the source of truth for what to delete: we select rows older
than the cutoff, delete each row's file (missing files are fine — maybe a
previous run already removed it, or the row's file was never written),
then delete the row itself. This is idempotent: re-running finds nothing
left to do. As a second pass, any day-folder that is now empty (or whose
folder-date is before the cutoff) is removed to avoid leaking empty
directories.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from argus.config import Config
from argus.db import Database

logger = logging.getLogger("argus.retention")

# (table, path column, images subdirectory)
_IMAGE_TABLES = (
    ("screenshots", "screenshots"),
    ("camera_frames", "camera"),
)


@dataclass
class PurgeResult:
    dry_run: bool
    cutoff: datetime
    files_removed: int = 0
    bytes_removed: int = 0
    rows_removed: int = 0
    dirs_removed: int = 0
    removed_paths: list[str] = field(default_factory=list)

    def summary(self) -> str:
        verb = "would remove" if self.dry_run else "removed"
        return (
            f"retention purge ({'dry-run' if self.dry_run else 'live'}): "
            f"cutoff={self.cutoff.isoformat()} "
            f"{verb} {self.files_removed} file(s), {self.bytes_removed} byte(s), "
            f"{self.rows_removed} db row(s), {self.dirs_removed} empty dir(s)"
        )


def _default_retention_days(config: Config) -> int:
    return int(config.get("storage", "retention_days", default=30))


def purge(
    db: Database,
    config: Config,
    retention_days: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> PurgeResult:
    """Purge raw images (files + rows) older than ``retention_days``.

    Never touches ``reports`` or ``window_events``. Safe to call
    repeatedly (idempotent) and safe if files/dirs are already missing.
    """
    if retention_days is None:
        retention_days = _default_retention_days(config)
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    cutoff_date = cutoff.date()

    result = PurgeResult(dry_run=dry_run, cutoff=cutoff)

    for table, subdir in _IMAGE_TABLES:
        _purge_table(db, config, table, subdir, cutoff, dry_run, result)

    # In dry-run mode files aren't actually deleted, so the DB-row pass
    # above already "saw" every file that has a matching row. Skip those
    # paths here to avoid double-counting the same file as both a row
    # deletion and an orphan when scanning directories.
    already_seen = set(result.removed_paths)
    for _, subdir in _IMAGE_TABLES:
        _purge_stale_dirs(config.images_dir / subdir, cutoff_date, dry_run, result, already_seen)

    # Recordings (mp4) have their own, shorter retention window and live in
    # a separate directory (config.recordings_dir/YYYY-MM-DD/*.mp4), not
    # under images_dir. Purge them on the same idempotent, DB-driven pass.
    rec_days = int(config.get("storage", "recordings_retention_days", default=7))
    rec_cutoff = now - timedelta(days=rec_days)
    _purge_table(db, config, "recordings", "recordings", rec_cutoff, dry_run, result)
    _purge_stale_dirs(
        config.recordings_dir, rec_cutoff.date(), dry_run, result, set(result.removed_paths)
    )

    logger.info(result.summary())
    return result


def _purge_table(
    db: Database,
    config: Config,
    table: str,
    subdir: str,
    cutoff: datetime,
    dry_run: bool,
    result: PurgeResult,
) -> None:
    cutoff_iso = cutoff.isoformat()
    with db.cursor() as cur:
        cur.execute(f"SELECT id, path FROM {table} WHERE ts < ?", (cutoff_iso,))
        rows = cur.fetchall()

    if not rows:
        return

    ids_to_delete: list[int] = []
    for row_id, path_str in rows:
        path = Path(path_str)
        size = 0
        try:
            if path.exists():
                size = path.stat().st_size
                if not dry_run:
                    path.unlink()
        except OSError:
            logger.warning("Could not remove image file %s (row id=%s)", path, row_id)
        else:
            result.files_removed += 1
            result.bytes_removed += size
            result.removed_paths.append(str(path))
        ids_to_delete.append(row_id)

    if not dry_run and ids_to_delete:
        with db.cursor() as cur:
            placeholders = ",".join("?" for _ in ids_to_delete)
            cur.execute(f"DELETE FROM {table} WHERE id IN ({placeholders})", ids_to_delete)

    result.rows_removed += len(ids_to_delete)


def _purge_stale_dirs(
    base_dir: Path,
    cutoff_date: date,
    dry_run: bool,
    result: PurgeResult,
    already_seen: set[str],
) -> None:
    """Remove now-empty (or wholly-stale) day-folders under ``base_dir``.

    Only ever considers folders whose name parses as a date strictly
    before ``cutoff_date`` — today's and recent folders are never
    touched, even if briefly empty.
    """
    if not base_dir.exists():
        return

    for entry in sorted(base_dir.iterdir()):
        if not entry.is_dir():
            continue
        try:
            dir_date = datetime.strptime(entry.name, "%Y-%m-%d").date()
        except ValueError:
            continue  # not a day-folder we manage; leave it alone

        if dir_date >= cutoff_date:
            continue  # today/recent — never bulk-remove

        remaining = [p for p in entry.iterdir() if str(p) not in already_seen]
        if remaining:
            # Rows for these files should already have been purged above;
            # anything left is an orphan (e.g. a file whose DB insert never
            # committed). Since the whole folder is older than the cutoff,
            # it's safe to remove.
            for leftover in remaining:
                try:
                    size = leftover.stat().st_size if leftover.is_file() else 0
                    if not dry_run and leftover.is_file():
                        leftover.unlink()
                except OSError:
                    logger.warning("Could not remove orphan file %s", leftover)
                    continue
                else:
                    if leftover.is_file():
                        result.files_removed += 1
                        result.bytes_removed += size
                        result.removed_paths.append(str(leftover))

        if dry_run:
            # Only "removable" if nothing unexpected (e.g. a nested dir)
            # would block the real rmdir.
            if all(p.is_file() for p in remaining):
                result.dirs_removed += 1
            continue

        try:
            entry.rmdir()
        except OSError:
            continue  # not empty (unexpected nested dir) or already gone; skip
        result.dirs_removed += 1
