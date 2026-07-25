"""Insurance policy registry and premium reconciliation.

Premium drift is why this exists. The July 2026 review found
homeowner-adjacent premiums had risen 42% since February
($3,626.10 -> $5,137.68/yr) with nothing surfacing it.

Ledger matching alone cannot catch that: Amica home, Fortegra, and Neptune
are all paid out of escrow inside the mortgage payment and never appear as
ledger payees. ``insurance_premium_observed`` is the hand-entered path that
makes those visible, which is why a policy with no observation reads as
*unverified* rather than as zero drift.
"""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Any

from bot.storage import connect

_FREQ_MULTIPLIER = {
    "annual": 1, "semiannual": 2, "quarterly": 4, "monthly": 12,
}

_POLICY_FIELDS = (
    "insurance_type", "provider", "policy_number", "covers", "through_employer",
    "coverage", "deductible", "premium_cents", "premium_frequency", "paid_via",
    "ledger_payee_norm", "sales_contact", "renewal_date", "comments", "active",
    "sort_order",
)

STALE_AFTER_DAYS = 365


def annualize(premium_cents: int | None, frequency: str | None) -> int | None:
    if premium_cents is None:
        return None
    return premium_cents * _FREQ_MULTIPLIER.get(frequency or "annual", 1)


def upsert_policy(db_path: Path | str, *, id: str | None = None, **fields: Any) -> str:
    unknown = set(fields) - set(_POLICY_FIELDS)
    if unknown:
        raise ValueError(f"unknown policy fields: {sorted(unknown)}")
    with connect(db_path) as con:
        if id is None:
            pid = uuid.uuid4().hex
            cols = ["id"] + list(fields)
            vals = [pid] + [fields[c] for c in fields]
            con.execute(
                f"INSERT INTO insurance_policy ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' for _ in cols)})",
                vals,
            )
            return pid
        if not fields:
            return id
        assignments = ", ".join(f"{c} = ?" for c in fields)
        con.execute(
            f"UPDATE insurance_policy SET {assignments} WHERE id = ?",
            [fields[c] for c in fields] + [id],
        )
        return id


def record_premium(
    db_path: Path | str,
    *,
    policy_id: str,
    as_of_date: str,
    amount_cents: int,
    source: str = "manual",
    note: str | None = None,
) -> int:
    with connect(db_path) as con:
        cur = con.execute(
            "INSERT INTO insurance_premium_observed "
            "(policy_id, as_of_date, amount_cents, source, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (policy_id, as_of_date, amount_cents, source, note),
        )
        return int(cur.lastrowid)


def _iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def list_policies(
    db_path: Path | str, *, today: date | None = None,
) -> list[dict[str, Any]]:
    """Registry rows with their latest observation and computed drift.

    ``drift_cents`` compares like with like: both sides annualized.
    ``unverified`` is True when there is no observation at all, or the most
    recent one is older than a year.
    """
    today = today or date.today()
    with connect(db_path) as con:
        policies = [dict(r) for r in con.execute(
            "SELECT * FROM insurance_policy ORDER BY active DESC, sort_order, insurance_type"
        )]
        observations = [dict(r) for r in con.execute(
            "SELECT policy_id, as_of_date, amount_cents, source "
            "FROM insurance_premium_observed ORDER BY as_of_date"
        )]

    latest: dict[str, dict[str, Any]] = {}
    for o in observations:
        latest[o["policy_id"]] = o      # ordered ascending, so last wins

    out = []
    for p in policies:
        expected = annualize(p["premium_cents"], p["premium_frequency"])
        obs = latest.get(p["id"])
        if obs is None:
            observed = observed_date = observed_source = None
            unverified = True
        else:
            observed = obs["amount_cents"]
            observed_date = _iso(obs["as_of_date"])
            observed_source = obs["source"]
            age = (today - date.fromisoformat(observed_date)).days
            unverified = age > STALE_AFTER_DAYS
        drift = (
            observed - expected
            if observed is not None and expected is not None
            else None
        )
        out.append({
            **p,
            "annual_premium_cents": expected,
            "observed_cents": observed,
            "observed_date": observed_date,
            "observed_source": observed_source,
            "drift_cents": drift,
            "unverified": unverified,
        })
    return out


def list_policies_for_snapshot(db_path: Path | str) -> list[dict[str, Any]]:
    """Active policies in the InsurancePolicy shape from types.ts:315-325."""
    with connect(db_path) as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM insurance_policy WHERE active = 1 "
            "ORDER BY sort_order, insurance_type"
        )]
    return [
        {
            "insurance_type": r["insurance_type"],
            "through_employer": (
                None if r["through_employer"] is None
                else bool(r["through_employer"])
            ),
            "provider": r["provider"] or "",
            "sales_contact": r["sales_contact"] or "",
            "coverage": r["coverage"] or "",
            "deductible": r["deductible"] or "",
            "annual_premium_cents": annualize(
                r["premium_cents"], r["premium_frequency"],
            ),
            "comments": r["comments"] or "",
            "renewal_date": r["renewal_date"] or "",
        }
        for r in rows
    ]
