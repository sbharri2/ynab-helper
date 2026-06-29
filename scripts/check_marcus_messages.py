"""One-off capture: pull every recent Marcus / Goldman Sachs email from
Steven's inbox (via the bot's IMAP) and dump headers + bodies to a dated
file on Google Drive, so we can work out the full set of alert formats
(the "scheduled" confirmation we already have, plus the "posted" /
"funds available" alerts that arrive when a transfer actually settles).

Why a local scheduled task and not a cloud routine: Marcus transaction
alerts land in sbharri2@gmail.com and are only readable through the bot's
IMAP App Password, which lives on this machine.

Run manually any time, or via the one-time YNAB-Marcus-Check scheduled task:
    python -m scripts.check_marcus_messages
    python -m scripts.check_marcus_messages --days 14
"""
from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

from bot.config import load_settings
from bot.gmail_imap import GmailIMAP, extract_body, extract_headers

# Marcus uses save.marcus.com for transactional alerts and e.marcus.com for
# marketing. We capture both senders but label them so the noise is obvious.
_QUERIES = [
    "from:save.marcus.com",
    "from:marcus",
    "from:goldman",
]
_OUT_DIR = Path(r"G:\My Drive\ynabclone")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=9,
                   help="Look-back window in days (default 9)")
    args = p.parse_args()

    settings = load_settings()
    acct = settings.gmail_accounts[0]
    pw = os.environ.get(acct.imap_password_env, "")
    if not pw:
        print(f"IMAP password env {acct.imap_password_env!r} not set; abort.")
        return

    window = f"newer_than:{args.days}d"
    seen: dict[bytes, dict] = {}
    with GmailIMAP(acct.email, pw) as imap:
        for q in _QUERIES:
            for uid in imap.search(f"{q} {window}"):
                if uid in seen:
                    continue
                msg = imap.fetch_message(uid)
                if msg is None:
                    continue
                h = extract_headers(msg)
                seen[uid] = {"headers": h, "body": extract_body(msg)}

    out = _OUT_DIR / f"marcus_capture_{date.today().isoformat()}.txt"
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"Marcus capture {date.today().isoformat()} — "
                f"{len(seen)} message(s), window={args.days}d\n")
        f.write("=" * 70 + "\n\n")
        for uid, m in seen.items():
            h = m["headers"]
            f.write(f"FROM:    {h.get('From','')}\n")
            f.write(f"SUBJECT: {h.get('Subject','')}\n")
            f.write(f"DATE:    {h.get('Date','')}\n")
            f.write("-" * 70 + "\n")
            f.write(m["body"] + "\n")
            f.write("\n" + "=" * 70 + "\n\n")

    print(f"wrote {len(seen)} Marcus message(s) -> {out}")
    for m in seen.values():
        h = m["headers"]
        print(f"  {h.get('Date','')[:31]:31s} | {h.get('From','')[:40]:40s}"
              f" | {h.get('Subject','')[:50]}")


if __name__ == "__main__":
    main()
