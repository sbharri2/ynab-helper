"""DB-backed store for investment holdings, valuation rounds, and values.

Replaces the Google Sheet -> xlsx -> parse-on-read pipeline that
``bot/investments.py`` implements. A *round* is a labelled group of
observations, not a claim that every value shares one date: each
``holding_value`` carries its own ``as_of_date``.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from bot.storage import connect
from datetime import date as _date

log = logging.getLogger(__name__)

_HOLDING_FIELDS = (
    "name", "owner", "kind", "account_type", "institution", "account_number",
    "tax_treatment", "ledger_account_id", "closed", "sort_order", "notes",
)

_VALUE_FIELDS = (
    "value_cents", "market_value_cents", "debt_cents", "vested_cents",
    "units", "unit_price_cents", "note",
)


def _new_id() -> str:
    return uuid.uuid4().hex


# ── Rounds ───────────────────────────────────────────────────────────────

def create_round(
    db_path: Path | str,
    *,
    label: str,
    as_of_date: str,
    seed_from_previous: bool = False,
) -> str:
    """Create a valuation round. Returns its id.

    With ``seed_from_previous`` the most recent earlier round's values are
    copied forward and flagged ``is_seeded=1``, so a new round starts as
    "confirm or change each number" rather than 23 blank fields. Seeded
    rows take the NEW round's date — carrying the old date forward is the
    fiction this whole design exists to remove.
    """
    rid = _new_id()
    with connect(db_path) as con:
        con.execute(
            "INSERT INTO snapshot_round (id, label, as_of_date) VALUES (?, ?, ?)",
            (rid, label, as_of_date),
        )
        if seed_from_previous:
            prev = con.execute(
                "SELECT id FROM snapshot_round WHERE as_of_date < ? "
                "ORDER BY as_of_date DESC LIMIT 1",
                (as_of_date,),
            ).fetchone()
            if prev is not None:
                con.execute(
                    """
                    INSERT INTO holding_value (
                        holding_id, round_id, as_of_date, value_cents,
                        market_value_cents, debt_cents, vested_cents,
                        units, unit_price_cents, source, is_seeded
                    )
                    SELECT hv.holding_id, ?, ?, hv.value_cents,
                           hv.market_value_cents, hv.debt_cents, hv.vested_cents,
                           hv.units, hv.unit_price_cents, 'manual', 1
                      FROM holding_value hv
                      JOIN holding h ON h.id = hv.holding_id
                     WHERE hv.round_id = ? AND h.closed = 0
                    """,
                    (rid, as_of_date, prev["id"]),
                )
    return rid


def list_rounds(db_path: Path | str) -> list[dict[str, Any]]:
    """Newest first."""
    with connect(db_path) as con:
        rows = con.execute(
            """
            SELECT r.id, r.label, r.as_of_date,
                   (SELECT COUNT(*) FROM holding_value hv
                     WHERE hv.round_id = r.id) AS value_count
              FROM snapshot_round r
             ORDER BY r.as_of_date DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def latest_round(db_path: Path | str) -> dict[str, Any] | None:
    rounds = list_rounds(db_path)
    return rounds[0] if rounds else None


# ── Holdings ─────────────────────────────────────────────────────────────

def upsert_holding(db_path: Path | str, *, id: str | None = None, **fields: Any) -> str:
    """Create (no id) or update (id given) a holding. Returns the id."""
    unknown = set(fields) - set(_HOLDING_FIELDS)
    if unknown:
        raise ValueError(f"unknown holding fields: {sorted(unknown)}")
    with connect(db_path) as con:
        if id is None:
            hid = _new_id()
            cols = ["id"] + list(fields)
            vals = [hid] + [fields[c] for c in fields]
            placeholders = ", ".join("?" for _ in cols)
            con.execute(
                f"INSERT INTO holding ({', '.join(cols)}) VALUES ({placeholders})",
                vals,
            )
            return hid
        if not fields:
            return id
        assignments = ", ".join(f"{c} = ?" for c in fields)
        con.execute(
            f"UPDATE holding SET {assignments} WHERE id = ?",
            [fields[c] for c in fields] + [id],
        )
        return id


def list_holdings(
    db_path: Path | str, *, include_closed: bool = True,
) -> list[dict[str, Any]]:
    where = "" if include_closed else "WHERE h.closed = 0"
    with connect(db_path) as con:
        rows = con.execute(
            f"""
            SELECT h.id, h.name, h.owner, h.kind, h.account_type,
                   h.institution, h.account_number, h.tax_treatment,
                   h.ledger_account_id, h.closed, h.sort_order, h.notes,
                   p.address, p.is_primary_residence, p.listed_price_cents,
                   p.escrow_cents, p.valuation_source
              FROM holding h
              LEFT JOIN property_detail p ON p.holding_id = h.id
              {where}
             ORDER BY h.closed, h.sort_order, h.name
            """
        ).fetchall()
    return [dict(r) for r in rows]


# ── Values ───────────────────────────────────────────────────────────────

_VALUE_COMPONENT_FIELDS = (
    "market_value_cents", "debt_cents", "vested_cents",
    "units", "unit_price_cents", "note",
)


def upsert_values(
    db_path: Path | str,
    *,
    round_id: str,
    values: list[dict[str, Any]],
    source: str = "manual",
) -> tuple[int, list[dict[str, Any]]]:
    """Insert-or-update one round's values in a single transaction.

    Each entry needs ``holding_id`` and ``value_cents``; ``as_of_date``
    defaults to the round's date. Writing a value always clears
    ``is_seeded`` — a number that was confirmed is no longer carried
    forward.

    Component columns (``market_value_cents``, ``debt_cents``,
    ``vested_cents``, ``units``, ``unit_price_cents``, ``note``) are only
    written when the entry's dict actually contains that key. A caller that
    omits a key leaves the existing stored value alone rather than nulling
    it out — the editor only ever sends the fields it actually touched
    (e.g. a plain "value" save with the breakout panel collapsed must not
    erase a previously-recorded market_value_cents/debt_cents). A key
    present with an explicit ``None`` still writes NULL — that's a
    deliberate clear, distinct from never having mentioned the column.

    Returns ``(count_written, changes)`` where ``changes`` lists
    ``{holding_id, from_cents, to_cents}`` for every row whose
    ``value_cents`` actually changed (including brand-new rows, where
    ``from_cents`` is ``None``) — the audit trail a Save should leave
    behind.
    """
    with connect(db_path) as con:
        row = con.execute(
            "SELECT as_of_date FROM snapshot_round WHERE id = ?", (round_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no such round: {round_id}")
        round_date = row["as_of_date"]
        round_date = round_date.isoformat() if hasattr(round_date, "isoformat") else round_date

        n = 0
        changes: list[dict[str, Any]] = []
        for v in values:
            unknown = set(v) - set(_VALUE_FIELDS) - {"holding_id", "as_of_date"}
            if unknown:
                raise ValueError(f"unknown value fields: {sorted(unknown)}")
            if "holding_id" not in v or v.get("value_cents") is None:
                raise ValueError("each value needs holding_id and value_cents")

            holding_id = v["holding_id"]
            as_of = v.get("as_of_date") or round_date

            prior = con.execute(
                "SELECT value_cents FROM holding_value "
                "WHERE holding_id = ? AND round_id = ?",
                (holding_id, round_id),
            ).fetchone()
            prior_cents = prior["value_cents"] if prior is not None else None

            insert_cols = [
                "holding_id", "round_id", "as_of_date", "value_cents",
                "source", "is_seeded",
            ]
            insert_vals: list[Any] = [
                holding_id, round_id, as_of, v["value_cents"], source, 0,
            ]
            set_parts = [
                "as_of_date = excluded.as_of_date",
                "value_cents = excluded.value_cents",
                "source = excluded.source",
                "is_seeded = 0",
            ]
            for col in _VALUE_COMPONENT_FIELDS:
                insert_cols.append(col)
                if col in v:
                    insert_vals.append(v[col])
                    set_parts.append(f"{col} = excluded.{col}")
                else:
                    insert_vals.append(None)
                    # Column not mentioned: leave whatever is already
                    # stored alone (no SET clause emitted for it).

            placeholders = ", ".join("?" for _ in insert_cols)
            con.execute(
                f"""
                INSERT INTO holding_value ({', '.join(insert_cols)})
                VALUES ({placeholders})
                ON CONFLICT (holding_id, round_id) DO UPDATE SET
                    {', '.join(set_parts)}
                """,
                insert_vals,
            )
            n += 1
            if prior_cents != v["value_cents"]:
                changes.append({
                    "holding_id": holding_id,
                    "from_cents": prior_cents,
                    "to_cents": v["value_cents"],
                })
    return n, changes


# Totals are COMPUTED, never stored. The sheet stored them and they drifted
# out of sync with their own inputs — two columns carried wrong header
# dates and the Feb 2026 column was labelled 2025.


def _as_iso(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _round_cells(db_path: Path | str) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    """Return (rounds oldest-first, {round_id: {holding_id: value_cents}})."""
    rounds = list(reversed(list_rounds(db_path)))
    with connect(db_path) as con:
        rows = con.execute(
            "SELECT round_id, holding_id, value_cents FROM holding_value"
        ).fetchall()
    by_round: dict[str, dict[str, int]] = {r["id"]: {} for r in rounds}
    for row in rows:
        by_round.setdefault(row["round_id"], {})[row["holding_id"]] = row["value_cents"]
    return rounds, by_round


def _seeded_flags(db_path: Path | str) -> dict[str, set[str]]:
    """{round_id: {holding_id, ...}} for every cell still carried forward
    (is_seeded=1) and never confirmed since."""
    with connect(db_path) as con:
        rows = con.execute(
            "SELECT round_id, holding_id FROM holding_value WHERE is_seeded = 1"
        ).fetchall()
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(row["round_id"], set()).add(row["holding_id"])
    return out


def compute_totals(db_path: Path | str) -> list[dict[str, Any]]:
    rounds, by_round = _round_cells(db_path)
    with connect(db_path) as con:
        primary = {
            r["holding_id"] for r in con.execute(
                "SELECT holding_id FROM property_detail WHERE is_primary_residence = 1"
            )
        }
        targets = [
            dict(r) for r in con.execute(
                "SELECT effective_year, combined_salary_cents, multiplier "
                "FROM savings_target ORDER BY effective_year"
            )
        ]

    # Zero primary-residence flags makes "Minus Home Equity" == "Total"
    # (silently overstating retirement readiness); two or more flags
    # double-subtracts. Neither is a number worth reporting — a visibly
    # absent cell is recoverable, a plausible wrong one is not.
    home_count_ok = len(primary) == 1
    if not home_count_ok:
        log.warning(
            "compute_totals: expected exactly 1 primary-residence holding, "
            "found %d (%s) — emitting Minus Home Equity as None",
            len(primary), sorted(primary),
        )

    total_cells: list[int] = []
    minus_home_cells: list[int | None] = []
    change_cells: list[float | None] = []
    target_cells: list[int | None] = []
    delta_cells: list[int | None] = []

    prev_total: int | None = None
    prev_date: _date | None = None

    for r in rounds:
        cells = by_round.get(r["id"], {})
        total = sum(cells.values())
        total_cells.append(total)
        if home_count_ok:
            home = sum(v for hid, v in cells.items() if hid in primary)
            minus_home_cells.append(total - home)
        else:
            minus_home_cells.append(None)

        as_of = _date.fromisoformat(_as_iso(r["as_of_date"]))
        # Both totals must be positive: a negative base raised to a
        # fractional exponent is a complex number, and round() rejects it.
        # A leveraged property makes a negative round total reachable.
        if prev_total is None or prev_total <= 0 or total <= 0 or prev_date is None:
            change_cells.append(None)
        else:
            days = (as_of - prev_date).days
            if days <= 0:
                change_cells.append(None)
            else:
                growth = total / prev_total
                change_cells.append(round((growth ** (365 / days) - 1) * 100, 2))
        prev_total, prev_date = total, as_of

        # Most recent target row at or before this round's year.
        applicable = [t for t in targets if t["effective_year"] <= as_of.year]
        if applicable:
            t = applicable[-1]
            target = int(round(t["combined_salary_cents"] * t["multiplier"]))
            target_cells.append(target)
            home_cell = minus_home_cells[-1]
            delta_cells.append(None if home_cell is None else home_cell - target)
        else:
            target_cells.append(None)
            delta_cells.append(None)

    return [
        {"label": "Total", "cells": total_cells},
        {"label": "Minus Home Equity", "cells": minus_home_cells},
        {"label": "Annual Change", "cells": change_cells},
        {"label": "Target Savings", "cells": target_cells},
        {"label": "Delta", "cells": delta_cells},
    ]


def build_snapshot(
    db_path: Path | str, *, round_id: str | None = None,
) -> dict[str, Any]:
    """Assemble the InvestmentSnapshot payload the Tauri pages consume.

    Shape is pinned by ``src/lib/types.ts:327-333`` — changing keys here
    breaks InvestmentsOverview/Holdings/Allocation/Insurance.
    """
    from bot import investments_insurance as ins

    rounds = list(reversed(list_rounds(db_path)))
    if round_id is not None:
        idx = next(
            (i for i, r in enumerate(rounds) if r["id"] == round_id), None,
        )
        if idx is None:
            raise ValueError(f"no such round: {round_id}")
        rounds = rounds[: idx + 1]
    if not rounds:
        return {
            "source_file": "database", "as_of": None,
            "holdings": [], "insurance": [], "totals_rows": [],
        }

    _, by_round = _round_cells(db_path)
    seeded_by_round = _seeded_flags(db_path)

    holdings_out: list[dict[str, Any]] = []
    for h in list_holdings(db_path, include_closed=True):
        values = [
            {
                "label": r["label"],
                "snapshot_date": _as_iso(r["as_of_date"]),
                "cents": by_round.get(r["id"], {}).get(h["id"], 0),
                # True when this cell was copied forward by
                # create_round(seed_from_previous=True) and never confirmed
                # since — a carried-forward number, not a fresh observation.
                # See Overview's "N carried forward" banner.
                "is_seeded": h["id"] in seeded_by_round.get(r["id"], set()),
            }
            for r in rounds
        ]
        holdings_out.append({
            "name": h["name"],
            "account_type": h["account_type"] or h["kind"],
            "account_number": h["account_number"] or "",
            "owner": h["owner"] or "",
            "values": values,
            "notes": h["notes"] or "",
            "is_real_estate": h["kind"] == "property",
        })

    totals = [
        {"label": t["label"], "cells": t["cells"][: len(rounds)]}
        for t in compute_totals(db_path)
    ]

    return {
        "source_file": "database",
        "as_of": _as_iso(rounds[-1]["as_of_date"]),
        "holdings": holdings_out,
        "insurance": ins.list_policies_for_snapshot(db_path),
        "totals_rows": totals,
    }
