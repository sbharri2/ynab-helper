"""One-shot wide-window Gmail poll to backfill the 2026-06-09 → 2026-06-14
ingest gap.

The bot was offline 2026-06-10 → 2026-06-14, so its in-process poll_once
hasn't run. Each email_source.query in config has `newer_than:2d` or
`newer_than:3d`, which is too narrow to catch up. This script rewrites
those windows to `newer_than:8d` in-memory only (no config file change),
then calls poll_once exactly the way the bot would.

Idempotent: ingest_signal dedupes on (account_id, posted_date, |amount|)
within ±2 days, so re-polling overlapping windows is safe.
"""
from __future__ import annotations

import logging
import re

from bot import gmail_watcher
from bot.config import load_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("backfill")

settings = load_settings()

newer_re = re.compile(r"newer_than:\d+d", re.I)
for src in settings.email_sources:
    before = src.query
    src.query = newer_re.sub("newer_than:8d", before)
    if before != src.query:
        log.info("widened %s: %r -> %r", src.name, before, src.query)
    else:
        log.info("kept    %s: %r (no newer_than: clause)", src.name, before)

n = gmail_watcher.poll_once(settings)
log.info("backfill done: %d new pending_order rows", n)
