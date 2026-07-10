"""Suggestions backtest harness (docs/suggestions-v2.md, honesty layer).

Replays every human-categorized pending_txn and scores:
  * recorded  — what production actually suggested at the time
  * memory_v2 — bot.suggest payee-memory (no leakage: history < txn_date)

Usage:
  .venv/Scripts/python.exe scripts/eval_suggestions.py [--db PATH]

Every layer change to bot/suggest.py or ingest._categorize must be
accompanied by a before/after run of this script in the commit message.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from bot import storage  # noqa: E402
from bot import suggest as sg  # noqa: E402


def load_eval_rows(db_path):
    with storage.connect(db_path) as con:
        return [dict(r) for r in con.execute(
            """SELECT pt.id, pt.payee, pt.amount_cents, pt.txn_date,
                      pt.suggested_category, pt.chosen_category
               FROM pending_txn pt
               WHERE pt.status = 'categorized'
                 AND pt.chosen_category IS NOT NULL
                 AND (pt.filed_by IS NULL OR pt.filed_by NOT LIKE 'auto%')
               ORDER BY pt.chosen_at""",
        ).fetchall()]


def pct(a, b):
    return f"{a}/{b} = {a / b:.0%}" if b else "n/a"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="C:/Users/Steven/ynabhelper/ynab_helper.db")
    ap.add_argument("--verbose", action="store_true",
                    help="print each miss for the memory_v2 layer")
    args = ap.parse_args()

    rows = load_eval_rows(args.db)
    print(f"eval set: {len(rows)} human-categorized pending_txns\n")

    with storage.connect(args.db) as con:
        names = {r["id"]: r["name"] for r in con.execute(
            "SELECT id, name FROM category")}

    # -- recorded production suggestion --------------------------------
    with_s = [r for r in rows if r["suggested_category"]]
    hit = sum(1 for r in with_s
              if r["suggested_category"] == r["chosen_category"])
    print("recorded production suggestion:")
    print(f"  coverage {pct(len(with_s), len(rows))}   "
          f"accuracy {pct(hit, len(with_s))}")

    # -- memory v2 (leak-free) ------------------------------------------
    cov = hits = strong_cov = strong_hits = 0
    misses = []
    for r in rows:
        d = r["txn_date"]
        as_of = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
        cat, tier = sg.memory_suggestion(args.db, r["payee"], as_of=as_of,
                                         amount_cents=r["amount_cents"])
        if cat is None:
            continue
        cov += 1
        ok = cat == r["chosen_category"]
        hits += ok
        if tier == "strong":
            strong_cov += 1
            strong_hits += ok
        if not ok:
            misses.append((r["payee"], names.get(cat), names.get(r["chosen_category"]), tier))
    print("\nmemory_v2 (payee memory, recency-weighted, leak-free):")
    print(f"  coverage {pct(cov, len(rows))}   accuracy {pct(hits, cov)}")
    print(f"  strong tier (auto-file grade): coverage {pct(strong_cov, len(rows))}   "
          f"accuracy {pct(strong_hits, strong_cov)}")

    # combined view: memory where it speaks, recorded suggestion elsewhere
    comb_cov = comb_hits = 0
    for r in rows:
        d = r["txn_date"]
        as_of = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
        cat, _tier = sg.memory_suggestion(args.db, r["payee"], as_of=as_of,
                                          amount_cents=r["amount_cents"])
        if cat is None:
            cat = r["suggested_category"]
        if cat is None:
            continue
        comb_cov += 1
        comb_hits += cat == r["chosen_category"]
    print("\nmemory_v2 → fallback to recorded LLM suggestion:")
    print(f"  coverage {pct(comb_cov, len(rows))}   accuracy {pct(comb_hits, comb_cov)}")

    if args.verbose and misses:
        print("\nmemory_v2 misses:")
        agg = defaultdict(int)
        for p, s, c, t in misses:
            agg[(s, c)] += 1
        for (s, c), n in sorted(agg.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>3}x  memory said {s!r:<36} human chose {c!r}")


if __name__ == "__main__":
    main()
