"""Consistent, Drive-safe backup of the live SQLite DB.

Why this exists: the live DB must stay on LOCAL disk. Google Drive (and
Dropbox/OneDrive) serve stale/partial pages for SQLite's random-access
reads, which corrupts a live file into "database disk image is malformed"
(happened 2026-07-07 — the data was fine, the Drive mount just couldn't
read it). So we keep the DB local and push *snapshots* to Drive instead.

Procedure (never lets Drive see a half-written file):
  1. sqlite3 online-backup API -> a LOCAL temp snapshot. Consistent even
     while the bot is mid-write; no locking of the live DB.
  2. PRAGMA integrity_check on the snapshot. Abort if not "ok".
  3. Copy the finished, closed file into the Drive backup dir under a
     timestamped name, then refresh a stable `ynab_helper_latest.db`.
  4. Prune timestamped snapshots to the newest --keep.

Run manually:  python -m scripts.backup_db
Scheduled:     see scripts/setup_scheduled_tasks.ps1 (YNAB-Helper-Backup)
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from bot.config import load_settings

LOG = logging.getLogger("backup_db")

DEFAULT_DEST = Path("G:/My Drive/ynabclone/backups")
STAMP_GLOB = "ynab_helper_*.db"
LATEST_NAME = "ynab_helper_latest.db"


def _snapshot(live: Path, tmp: Path) -> None:
    """Consistent copy of `live` into `tmp` via the online-backup API."""
    src = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)  # atomic, page-by-page; safe during writes
        finally:
            dst.close()
    finally:
        src.close()


def _verify(db: Path) -> None:
    con = sqlite3.connect(str(db))
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()
    if result != "ok":
        raise RuntimeError(f"integrity_check failed on snapshot: {result!r}")


def _prune(dest: Path, keep: int) -> list[Path]:
    stamped = sorted(dest.glob(STAMP_GLOB), key=lambda p: p.name)
    doomed = stamped[:-keep] if keep > 0 else []
    for old in doomed:
        old.unlink()
    return doomed


def run(dest: Path, keep: int, stamp: str) -> Path:
    settings = load_settings()
    live = Path(settings.paths.database)
    if not live.exists():
        raise FileNotFoundError(f"live DB not found: {live}")
    if "My Drive" in str(live) or "Dropbox" in str(live) or "OneDrive" in str(live):
        raise RuntimeError(
            f"live DB is on a cloud-sync path ({live}); it MUST be local. "
            "Fix config.yaml paths.database before backing up."
        )

    dest.mkdir(parents=True, exist_ok=True)
    # Stage the snapshot on LOCAL disk first, next to the live DB.
    tmp = live.with_name(f".backup_tmp_{stamp}.db")
    try:
        LOG.info("snapshotting %s -> %s", live, tmp)
        _snapshot(live, tmp)
        _verify(tmp)
        size_mb = tmp.stat().st_size / (1024 * 1024)

        target = dest / f"ynab_helper_{stamp}.db"
        LOG.info("copying verified snapshot (%.1f MB) -> %s", size_mb, target)
        shutil.copy2(tmp, target)          # only a complete file reaches Drive
        shutil.copy2(tmp, dest / LATEST_NAME)
    finally:
        tmp.unlink(missing_ok=True)

    pruned = _prune(dest, keep)
    if pruned:
        LOG.info("pruned %d old backup(s): %s", len(pruned), ", ".join(p.name for p in pruned))
    LOG.info("backup OK: %s", target)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST,
                        help=f"backup directory (default: {DEFAULT_DEST})")
    parser.add_argument("--keep", type=int, default=14,
                        help="number of timestamped snapshots to retain (default: 14)")
    parser.add_argument("--stamp", default=None,
                        help="override the timestamp (default: now, %%Y%%m%%d-%%H%%M%%S)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stamp = args.stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        run(args.dest, args.keep, stamp)
    except Exception as exc:  # noqa: BLE001 - top-level guard for the scheduled task
        LOG.error("backup FAILED: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
