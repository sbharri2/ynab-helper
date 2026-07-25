"""One-time import: snapshot xlsx -> DB.

Reuses ``bot/investments.py`` as the parser rather than reimplementing it,
so the two paths can be diffed against each other at cutover
(``scripts/verify_investments_import.py``).

Idempotent on round ``as_of_date`` and holding ``name``: re-running adds
nothing. An unparseable column header raises — this runs once, under
supervision, and a silently invented date is the exact class of error this
design exists to remove.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from bot import investments as parser
from bot import investments_insurance as ins
from bot import investments_store as store
from bot.storage import connect

log = logging.getLogger(__name__)

_KIND_BY_TYPE = {
    "401k": "retirement", "403b": "retirement", "ira": "retirement",
    "roth ira": "retirement", "roth": "retirement", "simple ira": "retirement",
    "pension": "retirement", "profit sharing": "retirement",
    "529": "education", "utma": "education",
    "hsa": "cash", "savings": "cash", "checking": "cash", "cd": "cash",
    "t-bills": "cash", "treasury": "cash",
    "crypto": "crypto", "bitcoin": "crypto", "ethereum": "crypto",
    "brokerage": "brokerage", "stock": "brokerage", "tod": "brokerage",
}

_TAX_BY_TYPE = {
    "roth ira": "roth", "roth": "roth", "401k": "pretax", "403b": "pretax",
    "ira": "pretax", "simple ira": "pretax", "hsa": "hsa", "529": "529",
}


def _classify(account_type: str) -> tuple[str, str | None]:
    """(kind, tax_treatment) from the sheet's free-text account type."""
    key = (account_type or "").strip().lower()
    if key in parser._REAL_ESTATE_TYPES:
        return "property", None
    for needle, kind in _KIND_BY_TYPE.items():
        if needle in key:
            return kind, _TAX_BY_TYPE.get(needle)
    return "other", None


def import_xlsx(
    db_path: Path | str, xlsx_path: Path | str | None = None,
) -> dict[str, Any]:
    path = Path(xlsx_path) if xlsx_path else parser.find_latest_snapshot()
    if path is None:
        raise ValueError(f"no xlsx files in {parser.SNAPSHOTS_DIR}")
    snap = parser.parse_snapshot(path)

    # ── Column dates. Every distinct snapshot_date across all holdings. ──
    dates: dict[str, str] = {}      # iso date -> label
    for h in snap["holdings"]:
        for v in h["values"]:
            if not v.get("snapshot_date"):
                raise ValueError(
                    f"no parseable date in column label {v.get('label')!r} — "
                    "fix the header before importing"
                )
            dates[v["snapshot_date"]] = v["label"]
    if not dates:
        raise ValueError("no parseable date columns found in the workbook")

    round_by_date: dict[str, str] = {}
    created_rounds = 0
    skipped: list[str] = []
    with connect(db_path) as con:
        for iso in sorted(dates):
            row = con.execute(
                "SELECT id FROM snapshot_round WHERE as_of_date = ?", (iso,),
            ).fetchone()
            if row is not None:
                round_by_date[iso] = row["id"]
                skipped.append(iso)
    for iso in sorted(dates):
        if iso not in round_by_date:
            round_by_date[iso] = store.create_round(
                db_path, label=dates[iso], as_of_date=iso,
            )
            created_rounds += 1

    # ── Holdings, keyed by name so re-import updates rather than duplicates ──
    with connect(db_path) as con:
        by_name = {
            r["name"]: r["id"] for r in con.execute("SELECT id, name FROM holding")
        }

    created_holdings = 0
    written_values = 0
    for order, h in enumerate(snap["holdings"]):
        kind, tax = _classify(h.get("account_type", ""))
        hid = by_name.get(h["name"])
        fields = dict(
            name=h["name"],
            owner=h.get("owner") or None,
            kind=kind,
            account_type=h.get("account_type") or None,
            account_number=h.get("account_number") or None,
            tax_treatment=tax,
            notes=h.get("notes") or None,
            sort_order=order,
        )
        if hid is None:
            hid = store.upsert_holding(db_path, **fields)
            by_name[h["name"]] = hid
            created_holdings += 1
        else:
            store.upsert_holding(db_path, id=hid, **fields)

        if kind == "property":
            with connect(db_path) as con:
                con.execute(
                    "INSERT OR IGNORE INTO property_detail "
                    "(holding_id, address, is_primary_residence) VALUES (?, ?, ?)",
                    (hid, h["name"], 1 if "mayfield" in h["name"].lower() else 0),
                )

        # Values are keyed by their own date — the parser compresses arrays
        # by dropping empty cells, so list position means nothing here.
        for v in h["values"]:
            rid = round_by_date[v["snapshot_date"]]
            store.upsert_values(
                db_path, round_id=rid, source="xlsx_import",
                values=[{
                    "holding_id": hid,
                    "value_cents": v["cents"],
                    "as_of_date": v["snapshot_date"],
                }],
            )
            written_values += 1

    # ── Insurance: registry + one baseline observation at the newest date ──
    newest_iso = max(dates)
    with connect(db_path) as con:
        known = {
            r["insurance_type"] for r in con.execute(
                "SELECT insurance_type FROM insurance_policy"
            )
        }
    created_policies = 0
    for order, p in enumerate(snap["insurance"]):
        if p["insurance_type"] in known:
            continue
        pid = ins.upsert_policy(
            db_path,
            insurance_type=p["insurance_type"],
            provider=p.get("provider") or None,
            through_employer=(
                None if p.get("through_employer") is None
                else int(bool(p["through_employer"]))
            ),
            coverage=p.get("coverage") or None,
            deductible=p.get("deductible") or None,
            premium_cents=p.get("annual_premium_cents"),
            premium_frequency="annual",
            paid_via="ledger",
            sales_contact=p.get("sales_contact") or None,
            renewal_date=p.get("renewal_date") or None,
            comments=p.get("comments") or None,
            sort_order=order,
        )
        created_policies += 1
        if p.get("annual_premium_cents") is not None:
            ins.record_premium(
                db_path, policy_id=pid, as_of_date=newest_iso,
                amount_cents=p["annual_premium_cents"], source="manual",
                note=f"baseline imported from {path.name}",
            )

    result = {
        "source_file": str(path),
        "rounds": created_rounds,
        "holdings": created_holdings,
        "values": written_values,
        "policies": created_policies,
        "skipped_rounds": skipped,
    }
    log.info("investments import: %s", result)
    return result
