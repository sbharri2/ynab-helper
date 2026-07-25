"""DB-backed store for investment holdings, valuation rounds, and values.

Replaces the Google Sheet -> xlsx -> parse-on-read pipeline that
``bot/investments.py`` implements. A *round* is a labelled group of
observations, not a claim that every value shares one date: each
``holding_value`` carries its own ``as_of_date``.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from bot.storage import connect

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
            SELECT h.*, p.address, p.is_primary_residence, p.listed_price_cents,
                   p.escrow_cents, p.valuation_source
              FROM holding h
              LEFT JOIN property_detail p ON p.holding_id = h.id
              {where}
             ORDER BY h.closed, h.sort_order, h.name
            """
        ).fetchall()
    return [dict(r) for r in rows]


# ── Values ───────────────────────────────────────────────────────────────

def upsert_values(
    db_path: Path | str,
    *,
    round_id: str,
    values: list[dict[str, Any]],
    source: str = "manual",
) -> int:
    """Insert-or-update one round's values in a single transaction.

    Each entry needs ``holding_id`` and ``value_cents``; ``as_of_date``
    defaults to the round's date. Writing a value always clears
    ``is_seeded`` — a number that was confirmed is no longer carried
    forward.
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
        for v in values:
            unknown = set(v) - set(_VALUE_FIELDS) - {"holding_id", "as_of_date"}
            if unknown:
                raise ValueError(f"unknown value fields: {sorted(unknown)}")
            if "holding_id" not in v or v.get("value_cents") is None:
                raise ValueError("each value needs holding_id and value_cents")
            con.execute(
                """
                INSERT INTO holding_value (
                    holding_id, round_id, as_of_date, value_cents,
                    market_value_cents, debt_cents, vested_cents,
                    units, unit_price_cents, source, is_seeded, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT (holding_id, round_id) DO UPDATE SET
                    as_of_date         = excluded.as_of_date,
                    value_cents        = excluded.value_cents,
                    market_value_cents = excluded.market_value_cents,
                    debt_cents         = excluded.debt_cents,
                    vested_cents       = excluded.vested_cents,
                    units              = excluded.units,
                    unit_price_cents   = excluded.unit_price_cents,
                    source             = excluded.source,
                    is_seeded          = 0,
                    note               = excluded.note
                """,
                (
                    v["holding_id"], round_id, v.get("as_of_date") or round_date,
                    v["value_cents"], v.get("market_value_cents"), v.get("debt_cents"),
                    v.get("vested_cents"), v.get("units"), v.get("unit_price_cents"),
                    source, v.get("note"),
                ),
            )
            n += 1
    return n
