"""Split the Exercise catch-all into per-person sport categories.

Steven, 2026-07-25. "Exercise" accumulated 196 transactions / $8,881 over
five years covering five unrelated activities. Steven's routing rules:

  tennis      = USTA + court fees (Cary Parks, Holly Springs, parks & rec)
  gym         = YMCA
  running     = 5K / race registrations
  golf        = Topgolf, Knights Play, Lag Shot -> the EXISTING Steven Golf
  peer-to-peer (Venmo/PayPal/Zelle) = tennis, "as I repay folks"

Anything the payee cannot place stays in Exercise rather than being
guessed at. Run with --apply to write; default is a dry run.

Categories are matched most-specific-first: the GOLF and RACE rules run
before the generic 'run'/'park' rules so "Lag Shot Golf" and
"Viking Dash Rale" don't get mis-binned.

Spec: docs/superpowers/specs/2026-07-25-personal-expenses-panel-design.md
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot import storage  # noqa: E402

DB_PATH = Path(__file__).resolve().parents[1] / "ynab_helper.db"
EXERCISE = "0c3aa81c-6ab3-48a1-a63c-bdd7164079cd"
STEVEN_GOLF = "local-bdba3435-752e-4f0e-94ed-d115a16f4d59"

# Ordered — first match wins, so specific patterns precede generic ones.
RULES: list[tuple[str, tuple[str, ...]]] = [
    ("GOLF", ("topgolf", "knights play", "lag shot", "golf")),
    ("GYM", ("ymca",)),
    ("RUNNING", (
        "signup", "rr*", "itsyourrace", "marathon", "5k", "10k",
        "dash", "race", "racing", "buffalo", "sweater", "monstermash",
        "nightnationrun", "donatelife", "bull moon", "livebold",
        "city of oaks", "runforlove", "beattheheat", "apex chamber",
        "ceg", "viking", "united mono", "catchi", "registration",
    )),
    ("TENNIS", (
        "usta", "tennis", "wwta", "sunsettennis", "enocta", "trc",
        "cary parks", "parks, rec", "parks rec", "holly springs",
        "act*parks", "venmo", "paypal", "zelle",
    )),
]


def classify(payee: str | None) -> str:
    p = (payee or "").lower()
    for label, needles in RULES:
        if any(n in p for n in needles):
            return label
    return "UNPLACED"


def main() -> int:
    apply = "--apply" in sys.argv

    with storage.connect(DB_PATH) as con:
        targets = {}
        for label, name in (("TENNIS", "Steven Tennis"),
                            ("GYM", "Allison Gym"),
                            ("RUNNING", "Allison Running")):
            row = con.execute(
                "SELECT id FROM category WHERE hidden = 0 AND name = ?",
                (name,),
            ).fetchone()
            if row is None:
                print(f"ABORT: category {name!r} does not exist yet — "
                      f"create it before running this")
                return 1
            targets[label] = row["id"]
        targets["GOLF"] = STEVEN_GOLF

        rows = con.execute(
            "SELECT id, posted_date, payee, amount_cents FROM ledger_txn "
            "WHERE category_id = ? ORDER BY posted_date", (EXERCISE,),
        ).fetchall()

        buckets: dict[str, list] = {}
        for r in rows:
            buckets.setdefault(classify(r["payee"]), []).append(r)

        print(f"{len(rows)} transactions currently in Exercise\n")
        for label in ("TENNIS", "GYM", "RUNNING", "GOLF", "UNPLACED"):
            items = buckets.get(label, [])
            cents = sum(-i["amount_cents"] for i in items)
            dest = ("stays in Exercise" if label == "UNPLACED"
                    else {"TENNIS": "Steven Tennis", "GYM": "Allison Gym",
                          "RUNNING": "Allison Running",
                          "GOLF": "Steven Golf"}[label])
            print(f"  {label:<10} {len(items):>4} txns  "
                  f"${cents / 100:>10,.2f}   -> {dest}")

        unplaced = buckets.get("UNPLACED", [])
        if unplaced:
            print("\n  UNPLACED detail (left in Exercise):")
            for r in unplaced:
                print(f"    {r['posted_date']}  {(r['payee'] or '(none)')[:38]:<40}"
                      f"${-r['amount_cents'] / 100:>8,.2f}")

        if not apply:
            print("\nDRY RUN — no changes written. Re-run with --apply.")
            return 0

        moved = 0
        for label, items in buckets.items():
            if label == "UNPLACED":
                continue
            con.executemany(
                "UPDATE ledger_txn SET category_id = ? WHERE id = ?",
                [(targets[label], r["id"]) for r in items],
            )
            moved += len(items)

    if apply:
        storage.audit(DB_PATH, "exercise_category_split", {
            "moved": moved, "left_in_exercise": len(unplaced),
            "reason": "2026-07-25 split Exercise into per-person sports",
        })
        print(f"\n{moved} transactions re-filed; "
              f"{len(unplaced)} left in Exercise.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
