"""Import missing Amazon charges from a Chase Amazon-card QFX export.

The Chase Amazon card's per-transaction detail is only partially in the ledger
(chase_alert emails + ynab_sync + history), so many individual Amazon purchases
have no ledger row for an order receipt to match against. A QFX export from
Chase is the authoritative complete list — this loads the MISSING Amazon charges
onto the Chase Amazon ledger account.

Safety:
  * Amazon-only by default (NAME contains AMAZON/AMZN) — leaves the rest of the
    card alone.
  * Idempotent two ways: skips a QFX txn whose FITID we already imported
    (source_email_id), AND skips one that matches an existing ledger row by
    amount + date (±3d) so we never double-count a charge already captured via
    chase_alert / ynab_sync / history.
  * Local-only: no ynab_txn_id, never pushed to YNAB.

Usage:
  .venv/Scripts/python.exe scripts/import_chase_amazon_qfx.py <file.qfx> [--dry-run] [--all]
"""
from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CHASE_AMAZON_ACCOUNT_ID = "41fc20dd-bb9b-41d9-a226-0bf649326696"
AMAZON_RE = re.compile(r"AMAZON|AMZN", re.I)

_TRN_RE = re.compile(r"<STMTTRN>(.*?)</STMTTRN>", re.S)


def _tag(block: str, name: str) -> str | None:
    m = re.search(rf"<{name}>([^<\r\n]*)", block)
    # OFX/SGML escapes &amp;/&apos;/&lt;&gt; — unescape or the same
    # merchant splits into two payee keys vs email-captured rows.
    return html.unescape(m.group(1).strip()) if m else None


def parse_qfx(text: str) -> list[dict]:
    out = []
    for block in _TRN_RE.findall(text):
        dtp = _tag(block, "DTPOSTED")
        amt = _tag(block, "TRNAMT")
        name = _tag(block, "NAME") or ""
        fitid = _tag(block, "FITID") or ""
        if not (dtp and amt):
            continue
        d = datetime.strptime(dtp[:8], "%Y%m%d").date()
        cents = int(round(float(amt) * 100))
        out.append({"date": d, "amount_cents": cents, "name": name, "fitid": fitid})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("qfx")
    ap.add_argument("--account-id", default=CHASE_AMAZON_ACCOUNT_ID)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true",
                    help="Import every card txn, not just Amazon.")
    ap.add_argument("--fuzz", type=int, default=3, help="date-match window in days")
    args = ap.parse_args()

    from bot import storage
    from bot.config import load_settings
    from bot.ingest import _dedupe_key

    repo = Path(__file__).resolve().parents[1]
    settings = load_settings(repo / "config.yaml")
    db = settings.paths.database

    text = Path(args.qfx).read_text(encoding="latin-1")
    txns = parse_qfx(text)
    if not args.all:
        txns = [t for t in txns if AMAZON_RE.search(t["name"])]
    txns.sort(key=lambda t: t["date"])

    with storage.connect(db) as con:
        acct = con.execute("SELECT name FROM account WHERE id=?", (args.account_id,)).fetchone()
        if not acct:
            print(f"ERROR: account {args.account_id} not found")
            return 1
        acct_name = acct[0]
        # Existing ledger rows on this account: (id, date, amount) + consumed flag.
        existing = [
            {"id": r[0], "date": date.fromisoformat(str(r[1])[:10]),
             "amount_cents": r[2], "used": False}
            for r in con.execute(
                "SELECT id, posted_date, amount_cents FROM ledger_txn "
                "WHERE account_id=? AND parent_txn_id IS NULL",
                (args.account_id,),
            )
        ]
        already_fitids = {
            r[0] for r in con.execute(
                "SELECT source_email_id FROM ledger_txn "
                "WHERE account_id=? AND source_signal='chase_qfx' "
                "  AND source_email_id IS NOT NULL",
                (args.account_id,),
            )
        }

    def find_existing(t) -> dict | None:
        best = None
        for e in existing:
            if e["used"] or e["amount_cents"] != t["amount_cents"]:
                continue
            gap = abs((e["date"] - t["date"]).days)
            if gap <= args.fuzz and (best is None or gap < best[0]):
                best = (gap, e)
        return best[1] if best else None

    to_insert, already_present, already_imported = [], 0, 0
    for t in txns:
        if t["fitid"] and t["fitid"] in already_fitids:
            already_imported += 1
            continue
        match = find_existing(t)
        if match:
            match["used"] = True
            already_present += 1
        else:
            to_insert.append(t)

    print(f"Account : {acct_name} ({args.account_id})")
    print(f"QFX file: {args.qfx}")
    print(f"Scope   : {'ALL card txns' if args.all else 'Amazon-only'}")
    print()
    print(f"QFX txns in scope        : {len(txns)}")
    print(f"  already in ledger      : {already_present}  (matched amount + date ±{args.fuzz}d)")
    print(f"  already imported (fitid): {already_imported}")
    print(f"  MISSING -> to insert   : {len(to_insert)}")
    print()
    outflows = [t for t in to_insert if t["amount_cents"] < 0]
    inflows = [t for t in to_insert if t["amount_cents"] >= 0]
    print(f"  of those: {len(outflows)} charges, {len(inflows)} refunds/credits")
    if to_insert:
        print("\n  sample of missing charges:")
        for t in to_insert[:12]:
            print(f"    {t['date']}  ${t['amount_cents']/100:>8.2f}  {t['name']}")

    if args.dry_run:
        print("\n(dry-run: nothing written)")
        return 0

    inserted = 0
    with storage.connect(db) as con:
        for t in to_insert:
            con.execute(
                """INSERT INTO ledger_txn
                     (account_id, posted_date, amount_cents, payee, memo,
                      category_id, cleared, source_signal, source_email_id,
                      dedupe_key)
                   VALUES (?, ?, ?, ?, ?, NULL, 'cleared', 'chase_qfx', ?, ?)""",
                (args.account_id, str(t["date"]), t["amount_cents"], t["name"],
                 "Chase Amazon QFX import", t["fitid"],
                 _dedupe_key(args.account_id, str(t["date"]), t["amount_cents"])),
            )
            inserted += 1
    print(f"\nInserted {inserted} rows onto {acct_name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
