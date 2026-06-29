"""Merge ledger_txn duplicates that ``merge_sync_dupes`` can't reach.

``merge_sync_dupes`` only pairs rows with the SAME amount within ±2 days.
That misses the two big real-world drift cases:

  * date drift — Amazon alerts on ship date, YNAB posts the charge 3-4
    days later; foreign-card settlement can lag the alert by up to 10 days.
  * tip / FX drift — the instant alert fires at the pre-auth amount and
    YNAB later syncs the settled (tipped / FX-converted) amount.

Both leave a synthetic CC-alert row (NULL / ledger:N ynab_txn_id) orphaned
next to the real YNAB-sync row. Same merge policy as merge_sync_dupes:

  * KEEP the synthetic row (rich CC-alert memo, maybe user categorization).
  * Pull the REAL row's data in: ynab_txn_id, payee, posted_date, the
    SETTLED amount (+ refreshed dedupe_key), category (if synthetic had
    none), cleared.
  * Delete the REAL row.

Safe by construction: only merges a pair when the synthetic and the real
row are each other's ONLY candidate (mutual degree 1). If a synthetic has
two plausible real partners (or vice-versa) we skip it — a leftover
duplicate is fine; a wrong merge that destroys a distinct charge is not.

Default dry-run. Re-run with --apply.
"""
from __future__ import annotations
import argparse
import sys
from datetime import date

from bot import storage
from bot.config import load_settings
from bot.ynab_full_sync import _payee_tokens

_LOOKBACK_DAYS = 200
_EXACT_WINDOW = 10
_TIP_WINDOW = 7
_TIP_MAX = 1.40


def _dt(s) -> date:
    return date.fromisoformat(str(s)[:10])


def _candidate(syn: dict, real: dict) -> str | None:
    """Return 'exact' | 'tip' if `real` is the same charge as `syn`, else None."""
    if syn["account_id"] != real["account_id"]:
        return None
    sa, ra = abs(syn["amount_cents"]), abs(real["amount_cents"])
    dd = abs((_dt(real["posted_date"]) - _dt(syn["posted_date"])).days)
    if sa == ra and dd <= _EXACT_WINDOW:
        return "exact"
    # tip/FX: outflow, settled (real) up to +40% of pre-auth (syn), payee overlap
    if (syn["amount_cents"] < 0 and real["amount_cents"] < 0
            and sa <= ra <= int(sa * _TIP_MAX) and dd <= _TIP_WINDOW
            and (_payee_tokens(syn["payee"]) & _payee_tokens(real["payee"]))):
        return "tip"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--db", default=None, help="override db path (e.g. a local copy)")
    args = parser.parse_args()
    settings = load_settings()
    db_path = args.db or settings.paths.database

    since = (date.today().toordinal() - _LOOKBACK_DAYS)
    since_iso = date.fromordinal(since).isoformat()

    with storage.connect(db_path) as con:
        rows = [dict(r) for r in con.execute(
            """SELECT lt.id, lt.account_id, lt.posted_date, lt.amount_cents,
                      lt.payee, lt.memo, lt.category_id, lt.cleared,
                      lt.ynab_txn_id, acc.name AS account_name
               FROM ledger_txn lt JOIN account acc ON acc.id = lt.account_id
               WHERE lt.posted_date >= ?
                 AND COALESCE(lt.is_split, 0) = 0
                 AND lt.parent_txn_id IS NULL""",
            (since_iso,),
        ).fetchall()]

    def is_syn(r):
        y = r["ynab_txn_id"]
        return y is None or str(y).startswith("ledger:")

    syns = [r for r in rows if is_syn(r)]
    reals = [r for r in rows if not is_syn(r)]

    # Build candidate edges, then keep only mutual degree-1 pairs.
    edges: list[tuple[dict, dict, str]] = []
    for s in syns:
        for rr in reals:
            kind = _candidate(s, rr)
            if kind:
                edges.append((s, rr, kind))

    from collections import Counter
    sdeg = Counter(id(s) for s, _, _ in edges)
    rdeg = Counter(id(rr) for _, rr, _ in edges)
    pairs = [(s, rr, k) for s, rr, k in edges
             if sdeg[id(s)] == 1 and rdeg[id(rr)] == 1]
    skipped_ambig = len(edges) - len(pairs)

    print(f"scanned {len(syns)} synthetic + {len(reals)} real rows "
          f"(last {_LOOKBACK_DAYS}d)")
    print(f"found {len(pairs)} unambiguous drift/tip dupe pairs "
          f"({skipped_ambig} ambiguous edges skipped)\n")

    for s, rr, k in sorted(pairs, key=lambda p: p[0]["posted_date"]):
        extra = (abs(rr["amount_cents"]) - abs(s["amount_cents"])) / 100
        tag = k if k == "exact" else f"tip +${extra:.2f}"
        print(f"  [{k:5}] {s['account_name'][:18]:18} "
              f"{(s['payee'] or '')[:24]:24} "
              f"{s['posted_date']} {s['amount_cents']/100:>9.2f}  ->  "
              f"YNAB {rr['posted_date']} {rr['amount_cents']/100:>9.2f} ({tag})")

    if not args.apply:
        print("\nDRY-RUN — pass --apply to write.")
        return 0

    print("\napplying merges…")
    merged = failed = 0
    for s, rr, _k in pairs:
        try:
            with storage.connect(db_path) as con:
                eff_cat = s["category_id"] or rr["category_id"]
                new_dedupe = (f"{s['account_id']}|{str(rr['posted_date'])[:10]}"
                              f"|{abs(rr['amount_cents'])}")
                # Park the real row's UUID under a sentinel first so the
                # keep-row UPDATE doesn't trip UNIQUE(ynab_txn_id).
                con.execute("UPDATE ledger_txn SET ynab_txn_id = ? WHERE id = ?",
                            (f"merged:{rr['id']}", rr["id"]))
                con.execute(
                    "UPDATE ledger_txn SET "
                    "  ynab_txn_id = ?, payee = COALESCE(?, payee), "
                    "  posted_date = ?, amount_cents = ?, dedupe_key = ?, "
                    "  category_id = ?, cleared = COALESCE(?, cleared), "
                    "  updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (rr["ynab_txn_id"], rr["payee"], rr["posted_date"],
                     rr["amount_cents"], new_dedupe, eff_cat, rr["cleared"],
                     s["id"]),
                )
                # Re-point anything FK-referencing the real row onto the
                # kept row before deleting it (else FK constraint blocks the
                # delete). 'OR IGNORE' drops a signal that would collide with
                # the keep row's UNIQUE(signal_kind, email_id); the follow-up
                # DELETE clears any such leftover.
                con.execute(
                    "UPDATE OR IGNORE ledger_signal SET ledger_txn_id = ? "
                    "WHERE ledger_txn_id = ?", (s["id"], rr["id"]))
                con.execute("DELETE FROM ledger_signal WHERE ledger_txn_id = ?",
                            (rr["id"],))
                con.execute("UPDATE ledger_txn SET parent_txn_id = ? "
                            "WHERE parent_txn_id = ?", (s["id"], rr["id"]))
                con.execute("DELETE FROM ledger_txn WHERE id = ?", (rr["id"],))
            merged += 1
        except Exception as e:  # noqa: BLE001
            print(f"  failed keep={s['id']} drop={rr['id']}: {e}")
            failed += 1

    storage.audit(db_path, "drift_dupes_merged", {
        "merged": merged, "failed": failed, "total_pairs": len(pairs),
    })
    print(f"\nmerged {merged} pairs, {failed} failed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
