"""Pull the oldest unreviewed raw_email_sample for a sender and write it to
disk as a fixture for parser development.

Usage:
    python -m scripts.extract_fixture --sender chase_amazon_alerts
    python -m scripts.extract_fixture --sender chase_amazon_alerts --out tests/fixtures/chase_alert_001.eml
    python -m scripts.extract_fixture --sender chase_amazon_alerts --include-html

When `--out` is omitted, picks the next available `<sender>_NNN.eml` filename
in `tests/fixtures/`. Always marks the sample reviewed=1 after extraction so
later runs target fresh content.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from bot import storage
from bot.config import load_settings

log = logging.getLogger("extract_fixture")

FIXTURES_DIR = Path("tests/fixtures")


def _next_available_path(sender: str) -> Path:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    base = sender
    for n in range(1, 1000):
        candidate = FIXTURES_DIR / f"{base}_{n:03d}.eml"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"too many fixtures for {sender}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sender", required=True,
                   help="sender_label to pull from (e.g. chase_amazon_alerts)")
    p.add_argument("--out", type=Path, default=None,
                   help="output path; defaults to tests/fixtures/<sender>_NNN.eml")
    p.add_argument("--include-html", action="store_true",
                   help="write the HTML body instead of plaintext (some senders are HTML-only)")
    p.add_argument("--dry-run", action="store_true",
                   help="don't write a file or mark reviewed; just print")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = load_settings()
    db_path = settings.paths.database

    with storage.connect(db_path) as con:
        row = con.execute(
            """SELECT id, sender, subject, date_header, body_text, body_html, snippet
               FROM raw_email_sample
               WHERE sender_label = ? AND reviewed = 0
               ORDER BY inserted_at ASC LIMIT 1""",
            (args.sender,),
        ).fetchone()

    if row is None:
        log.error("no unreviewed samples for sender_label=%s", args.sender)
        return 1

    d = dict(row)
    body = d["body_html"] if args.include_html else (d["body_text"] or d["body_html"])
    if not body:
        log.error("sample #%d has no body content", d["id"])
        return 1

    out_path = args.out or _next_available_path(args.sender)
    log.info("Sample #%d  from=%s  subject=%r", d["id"], d["sender"], d["subject"])
    log.info("Body source: %s  length=%d",
             "html" if args.include_html or not d["body_text"] else "text",
             len(body))
    log.info("Output path: %s", out_path)

    if args.dry_run:
        log.info("--dry-run: not writing, not marking reviewed")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Mini-EML header so test fixtures retain the metadata needed by parsers
    # that consume both subject and date_header.
    header = (
        f"Subject: {d['subject'] or ''}\n"
        f"From: {d['sender']}\n"
        f"Date: {d['date_header'] or ''}\n"
        f"\n"
    )
    out_path.write_text(header + body, encoding="utf-8")
    log.info("Wrote %d bytes to %s", out_path.stat().st_size, out_path)

    with storage.connect(db_path) as con:
        con.execute(
            "UPDATE raw_email_sample SET reviewed = 1 WHERE id = ?",
            (d["id"],),
        )
    log.info("Marked sample #%d reviewed=1", d["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
