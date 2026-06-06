"""One-shot: pull complete YNAB budget data via API and save to
_LOCAL_SECRETS_/ynab_history/ for future Claude sessions to mine.

Outputs:
  transactions.csv      every transaction (id, date, payee, category, amount, memo, account, cleared, approved, flag)
  transactions.json     same, structured
  accounts.json         all accounts with balances
  categories.json       all category groups + categories with budgeted/activity/balance
  payees.json           all known payees
  months.json           per-month budget snapshots (categories.activity)
  summary.txt           quick stats so context can be loaded cheaply
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import ynab
from ynab.api.accounts_api import AccountsApi
from ynab.api.categories_api import CategoriesApi
from ynab.api.months_api import MonthsApi
from ynab.api.payees_api import PayeesApi
from ynab.api.transactions_api import TransactionsApi

from bot.config import load_settings

OUT = Path("_LOCAL_SECRETS_/ynab_history")
OUT.mkdir(parents=True, exist_ok=True)


def _to_native(v):
    """ynab SDK returns UUIDs, dates, decimals — coerce so json.dump works."""
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def main():
    s = load_settings()
    cfg = ynab.Configuration(access_token=s.ynab_token)
    client = ynab.ApiClient(cfg)
    plan_id = s.ynab.budget_id

    # ---- Accounts ----
    print("Fetching accounts...")
    accts = AccountsApi(client).get_accounts(plan_id=plan_id).data.accounts
    acct_records = []
    acct_id_to_name = {}
    for a in accts:
        rec = {
            "id": str(a.id),
            "name": a.name,
            "type": str(a.type),
            "on_budget": a.on_budget,
            "closed": a.closed,
            "balance": a.balance / 1000.0,
            "cleared_balance": a.cleared_balance / 1000.0,
            "uncleared_balance": a.uncleared_balance / 1000.0,
            "transfer_payee_id": _to_native(getattr(a, "transfer_payee_id", None)),
            "deleted": a.deleted,
        }
        acct_records.append(rec)
        acct_id_to_name[rec["id"]] = rec["name"]
    (OUT / "accounts.json").write_text(json.dumps(acct_records, indent=2))
    print(f"  {len(acct_records)} accounts saved")

    # ---- Categories ----
    print("Fetching categories...")
    cat_groups = CategoriesApi(client).get_categories(plan_id=plan_id).data.category_groups
    cat_records = []
    cat_id_to_name = {}
    for g in cat_groups:
        for c in g.categories:
            rec = {
                "id": str(c.id),
                "name": c.name,
                "group_id": str(g.id),
                "group_name": g.name,
                "hidden": c.hidden,
                "deleted": c.deleted,
                "budgeted": c.budgeted / 1000.0,
                "activity": c.activity / 1000.0,
                "balance": c.balance / 1000.0,
                "goal_type": _to_native(getattr(c, "goal_type", None)),
                "goal_target": (getattr(c, "goal_target", None) or 0) / 1000.0
                                 if getattr(c, "goal_target", None) is not None else None,
            }
            cat_records.append(rec)
            cat_id_to_name[rec["id"]] = rec["name"]
    (OUT / "categories.json").write_text(json.dumps(cat_records, indent=2))
    print(f"  {len(cat_records)} categories saved")

    # ---- Payees ----
    print("Fetching payees...")
    payees = PayeesApi(client).get_payees(plan_id=plan_id).data.payees
    payee_records = []
    payee_id_to_name = {}
    for p in payees:
        rec = {
            "id": str(p.id),
            "name": p.name,
            "transfer_account_id": _to_native(getattr(p, "transfer_account_id", None)),
            "deleted": p.deleted,
        }
        payee_records.append(rec)
        payee_id_to_name[rec["id"]] = rec["name"]
    (OUT / "payees.json").write_text(json.dumps(payee_records, indent=2))
    print(f"  {len(payee_records)} payees saved")

    # ---- Transactions (FULL HISTORY) ----
    # YNAB's no-since_date default windows to last ~12 months. Pass an
    # ancient date to force full-history return.
    print("Fetching ALL transactions (since 2000-01-01) ...")
    txns = TransactionsApi(client).get_transactions(
        plan_id=plan_id, since_date="2000-01-01",
    ).data.transactions
    txn_records = []
    for t in txns:
        rec = {
            "id": str(t.id),
            "date": t.var_date.isoformat(),
            "amount": t.amount / 1000.0,
            "memo": t.memo or "",
            "cleared": str(t.cleared),
            "approved": t.approved,
            "flag_color": _to_native(t.flag_color),
            "account_id": str(t.account_id),
            "account_name": acct_id_to_name.get(str(t.account_id), "???"),
            "payee_id": _to_native(t.payee_id),
            "payee_name": t.payee_name or "",
            "category_id": _to_native(t.category_id),
            "category_name": cat_id_to_name.get(str(t.category_id) if t.category_id else "", ""),
            "transfer_account_id": _to_native(t.transfer_account_id),
            "deleted": t.deleted,
            "import_id": _to_native(getattr(t, "import_id", None)),
        }
        txn_records.append(rec)
    (OUT / "transactions.json").write_text(json.dumps(txn_records, indent=2))

    # CSV for easy scan
    with open(OUT / "transactions.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(txn_records[0].keys()) if txn_records else [])
        w.writeheader()
        w.writerows(txn_records)
    print(f"  {len(txn_records)} transactions saved (json + csv)")

    # ---- Quick summary stats ----
    print("Building summary...")
    dates = [t["date"] for t in txn_records if not t["deleted"]]
    by_cat = Counter()
    by_account = Counter()
    by_payee = Counter()
    by_year_month = Counter()
    spend_by_cat = defaultdict(float)
    for t in txn_records:
        if t["deleted"] or t["transfer_account_id"]:
            continue
        cat = t["category_name"] or "(uncategorized)"
        by_cat[cat] += 1
        by_account[t["account_name"]] += 1
        by_payee[t["payee_name"]] += 1
        by_year_month[t["date"][:7]] += 1
        if t["amount"] < 0:
            spend_by_cat[cat] += -t["amount"]

    lines = []
    lines.append(f"YNAB history dump")
    lines.append(f"=================")
    lines.append(f"Date range: {min(dates)} .. {max(dates)}")
    lines.append(f"Total transactions: {len(txn_records)}")
    lines.append(f"  non-transfer, non-deleted: {sum(1 for t in txn_records if not t['deleted'] and not t['transfer_account_id'])}")
    lines.append(f"")
    lines.append(f"Accounts: {len(acct_records)} ({sum(1 for a in acct_records if not a['closed'] and not a['deleted'])} active)")
    lines.append(f"Categories: {len(cat_records)} ({sum(1 for c in cat_records if not c['hidden'] and not c['deleted'])} active)")
    lines.append(f"Payees: {len(payee_records)}")
    lines.append(f"")
    lines.append(f"Top 20 spending categories (all-time $):")
    for cat, amt in sorted(spend_by_cat.items(), key=lambda x: -x[1])[:20]:
        lines.append(f"  ${amt:>12,.0f}  {cat}")
    lines.append(f"")
    lines.append(f"Top 15 payees by transaction count:")
    for p, n in by_payee.most_common(15):
        lines.append(f"  {n:>5}  {p or '(no payee)'}")
    lines.append(f"")
    lines.append(f"Transactions per month (last 12):")
    for ym in sorted(by_year_month)[-12:]:
        lines.append(f"  {ym}: {by_year_month[ym]}")
    (OUT / "summary.txt").write_text("\n".join(lines))

    print()
    print("Done. Output:")
    for p in sorted(OUT.iterdir()):
        size_kb = p.stat().st_size / 1024
        print(f"  {p.name}  ({size_kb:,.0f} KB)")


if __name__ == "__main__":
    main()
