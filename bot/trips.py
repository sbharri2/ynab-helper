"""Suggestions-v2 trip windows (docs/suggestions-v2.md layer 3).

A lodging charge (or an explicit "we're traveling until ..." group
message) opens a candidate trip. ONE group confirmation arms it; while
armed, away-from-home charges inside the window file as Vacation with
provenance ``filed_by='auto_trip'``.

Everything here is deterministic. City reasoning applies ONLY to raw
card-alert payees (citi_alert / chase_alert) — cleaned YNAB-history
names carry no city and must never be geo-judged (measured 2026-07-10:
a naive city rule on clean names was 34% accurate).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from bot import storage

log = logging.getLogger(__name__)

VACATION_CATEGORY_NAME = "Vacation"

# Home metro — a payee containing any of these is a home-area charge.
HOME_CITIES = (
    "HOLLY SPRINGS", "APEX", "CARY", "RALEIGH", "DURHAM", "FUQUAY",
    "MORRISVILLE", "GARNER", "WAKE FOREST", "CHAPEL HILL", "KNIGHTDALE",
    "CLAYTON",
)

# Raw card alerts carry "MERCHANT CITY ST/USA"; only these sources may be
# geo-judged.
CITY_BEARING_SOURCES = {"citi_alert", "chase_alert"}

# Payees that open a candidate trip.
_LODGING_RE = re.compile(
    r"\b(MARRIOTT|HILTON|HYATT|WESTIN|SHERATON|HOLIDAY INN|HAMPTON INN|"
    r"COURTYARD|RESIDENCE INN|EMBASSY SUITES|DOUBLETREE|WYNDHAM|RITZ|"
    r"AIRBNB|VRBO|HOMEAWAY|BOOKING\.COM|EXPEDIA|HOTELS\.COM|"
    r"HOTEL|MOTEL|RESORT|LODGE|CAMPGROUND|KOA)\b", re.I,
)

# Categories a trip window may override to Vacation. Bills, savings,
# medical etc. keep their category even on the road.
ELIGIBLE_BASE_NAMES = {
    "Dining Out/Entertainment", "Transportation", "Groceries", "Exercise",
}

# Card-not-present giveaways — never Vacation-file these even away.
_ONLINE_RE = re.compile(r"\.COM|\.NET|ONLINE|\bWWW\b|BILL PAY|AUTOPAY", re.I)

_TRIP_PRE_DAYS = 1     # window opens the day before the lodging charge
_TRIP_POST_DAYS = 5    # ... and runs a few days past it; extendable later


def _as_date(v) -> date | None:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def is_lodging_payee(payee: str | None) -> bool:
    return bool(payee) and bool(_LODGING_RE.search(payee))


def is_home_payee(payee: str) -> bool:
    up = payee.upper()
    return any(c in up for c in HOME_CITIES)


def looks_online(payee: str) -> bool:
    return bool(_ONLINE_RE.search(payee))


def recurring_same_payee(db_path: Path | str, payee: str, *,
                         min_count: int = 3) -> bool:
    """≥N prior ledger rows with this exact normalized key = a recurring
    (usually online/HQ-city) merchant — Cupertino on APPLE.COM/BILL is
    not travel. Novel-ish payees pass."""
    from bot.suggest import normalize_payee
    key = normalize_payee(payee)
    if not key:
        return False
    n = 0
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT payee FROM ledger_txn WHERE payee IS NOT NULL",
        ).fetchall()
    for r in rows:
        if normalize_payee(r["payee"]) == key:
            n += 1
            if n >= min_count:
                return True
    return False


# ---------------------------------------------------------------------------
# Trip rows
# ---------------------------------------------------------------------------

def overlapping_trip(db_path: Path | str, start: date, end: date):
    with storage.connect(db_path) as con:
        return con.execute(
            """SELECT * FROM trip
               WHERE state IN ('candidate','confirmed')
                 AND start_date <= ? AND end_date >= ?
               ORDER BY id DESC LIMIT 1""",
            (end, start),
        ).fetchone()


def active_confirmed_trip(db_path: Path | str, on: date):
    with storage.connect(db_path) as con:
        return con.execute(
            """SELECT * FROM trip WHERE state = 'confirmed'
                 AND start_date <= ? AND end_date >= ?
               ORDER BY id DESC LIMIT 1""",
            (on, on),
        ).fetchone()


def create_candidate_from_charge(db_path: Path | str, *, pt_id: int | None,
                                 payee: str, txn_date) -> int | None:
    """A lodging charge opens a candidate trip window unless one already
    covers those dates. Returns the new trip id (None when suppressed)."""
    d = _as_date(txn_date)
    if d is None:
        return None
    start = d - timedelta(days=_TRIP_PRE_DAYS)
    end = d + timedelta(days=_TRIP_POST_DAYS)
    if overlapping_trip(db_path, start, end) is not None:
        return None
    with storage.connect(db_path) as con:
        cur = con.execute(
            """INSERT INTO trip (state, start_date, end_date,
                                 detected_from_pt, detect_payee)
               VALUES ('candidate', ?, ?, ?, ?)""",
            (start, end, pt_id, payee),
        )
        trip_id = cur.lastrowid
    storage.audit(db_path, "trip_candidate_created", {
        "trip_id": trip_id, "start": str(start), "end": str(end),
        "detect_payee": payee, "pt_id": pt_id,
    })
    return trip_id


def create_confirmed_manual(db_path: Path | str, *, start: date, end: date,
                            by: str) -> int:
    """Explicit 'we're traveling until X' — no confirmation round-trip."""
    with storage.connect(db_path) as con:
        cur = con.execute(
            """INSERT INTO trip (state, start_date, end_date, confirmed_by,
                                 resolved_at)
               VALUES ('confirmed', ?, ?, ?, ?)""",
            (start, end, by, storage._utcnow()),
        )
        trip_id = cur.lastrowid
    storage.audit(db_path, "trip_confirmed", {
        "trip_id": trip_id, "start": str(start), "end": str(end),
        "by": by, "manual": True,
    })
    return trip_id


def set_trip_state(db_path: Path | str, trip_id: int, state: str,
                   by: str | None = None) -> None:
    with storage.connect(db_path) as con:
        con.execute(
            "UPDATE trip SET state = ?, confirmed_by = COALESCE(?, confirmed_by), "
            "resolved_at = ? WHERE id = ?",
            (state, by, storage._utcnow(), trip_id),
        )
    storage.audit(db_path, f"trip_{state}", {"trip_id": trip_id, "by": by})


# ---------------------------------------------------------------------------
# The filing rule
# ---------------------------------------------------------------------------

def trip_vacation_category(db_path: Path | str, *, payee: str, txn_date,
                           source: str,
                           base_category_name: str | None) -> str | None:
    """Return the Vacation category id when this charge should file as
    Vacation under a confirmed trip window; else None."""
    if source not in CITY_BEARING_SOURCES:
        return None
    d = _as_date(txn_date)
    if d is None or not payee:
        return None
    trip = active_confirmed_trip(db_path, d)
    if trip is None:
        return None
    if is_home_payee(payee) or looks_online(payee):
        return None
    if base_category_name is not None \
            and base_category_name not in ELIGIBLE_BASE_NAMES \
            and not is_lodging_payee(payee):
        return None
    if recurring_same_payee(db_path, payee):
        return None
    with storage.connect(db_path) as con:
        row = con.execute("SELECT id FROM category WHERE name = ?",
                          (VACATION_CATEGORY_NAME,)).fetchone()
    return row["id"] if row else None


def apply_trip_to_pending(db_path: Path | str, trip_row) -> list[dict]:
    """Retro-file pending txns inside a just-confirmed window. Returns the
    filed items for the receipt message.

    HOLD-lane rows (spec 2026-07-25: large Amazon charges awaiting their
    order email) are excluded by the query below — nothing auto-commits a
    category at/above the large-charge threshold, and a trip rule is not
    an exception. They stay in HOLD and get asked normally once promoted
    or expired.

    That lane check alone isn't sufficient, though: a large Amazon charge
    that already got PROMOTED out of HOLD (TTL expiry or a matched
    receipt) is 'hot'/'cold', not 'hold', and status stays 'pending' until
    a human answers. Guard on payee + amount too (2026-07-26 follow-up
    review, Issue C) so a promoted-but-unanswered large Amazon charge
    still can't be auto-filed here — before this, only the incidental
    recurring_same_payee() True-for-Amazon check stood in the way, which
    is exactly the coincidence-not-a-guard the original finding flagged.
    (Amount alone isn't enough either: ingest._is_large_amazon_charge only
    tests magnitude, so it has to be paired with ingest._is_amazon_payee —
    otherwise a legitimately large NON-Amazon vacation charge, e.g. a
    $400 hotel night, would be wrongly blocked from auto-filing too.)"""
    from bot import ingest as _ingest
    from bot.group_chat import resolve_open_questions_for_item
    filed: list[dict] = []
    with storage.connect(db_path) as con:
        cands = [dict(r) for r in con.execute(
            """SELECT pt.id, pt.payee, pt.amount_cents, pt.txn_date,
                      lt.source_signal AS source, c.name AS base_name,
                      pt.ynab_txn_id
               FROM pending_txn pt
               LEFT JOIN ledger_txn lt
                 ON lt.id = CAST(REPLACE(pt.ynab_txn_id,'ledger:','') AS INTEGER)
                AND pt.ynab_txn_id LIKE 'ledger:%'
               LEFT JOIN category c ON c.id = pt.suggested_category
               WHERE pt.status = 'pending'
                 AND pt.queue_lane <> 'hold'
                 AND pt.txn_date BETWEEN ? AND ?""",
            (trip_row["start_date"], trip_row["end_date"]),
        ).fetchall()]
    for pt in cands:
        if (_ingest._is_amazon_payee(pt["payee"] or "")
                and _ingest._is_large_amazon_charge(pt["amount_cents"], None)):
            continue
        src = pt["source"]
        if src is None:
            # pending row not linked via ledger: prefix — look it up by
            # the ynab txn id instead.
            with storage.connect(db_path) as con:
                r = con.execute(
                    "SELECT source_signal FROM ledger_txn WHERE ynab_txn_id = ?",
                    (pt["ynab_txn_id"],),
                ).fetchone()
            src = r["source_signal"] if r else ""
        vac = trip_vacation_category(
            db_path, payee=pt["payee"] or "", txn_date=pt["txn_date"],
            source=src or "", base_category_name=pt["base_name"])
        if vac is None:
            continue
        with storage.connect(db_path) as con:
            con.execute(
                "UPDATE pending_txn SET chosen_category = ?, chosen_at = ?, "
                "status = 'categorized', filed_by = 'auto_trip' WHERE id = ?",
                (vac, storage._utcnow(), pt["id"]),
            )
            yid = pt["ynab_txn_id"] or ""
            if yid.startswith("ledger:"):
                try:
                    con.execute(
                        "UPDATE ledger_txn SET category_id = ?, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (vac, int(yid.split(":", 1)[1])),
                    )
                except ValueError:
                    pass
            elif yid:
                con.execute(
                    "UPDATE ledger_txn SET category_id = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE ynab_txn_id = ?",
                    (vac, yid),
                )
        resolve_open_questions_for_item(db_path, "txn", pt["id"], "auto_trip")
        storage.audit(db_path, "auto_filed", {
            "pending_txn_id": pt["id"], "payee": pt["payee"],
            "category_id": vac, "method": "trip", "trip_id": trip_row["id"],
        })
        filed.append(pt)
    return filed


# ---------------------------------------------------------------------------
# "we're traveling until ..." parsing
# ---------------------------------------------------------------------------

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}
_WEEKDAYS = {w.lower(): i for i, w in enumerate(
    ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
     "Saturday", "Sunday"])}

_TRAVEL_RE = re.compile(
    r"\b(on vacation|on a trip|travel+ing|out of town)\b.*?"
    r"\b(?:until|through|thru|till|til)\s+(.+)$", re.I)


def parse_travel_message(text: str, *, today: date | None = None
                         ) -> tuple[date, date] | None:
    """'we're traveling until the 26th' → (today, that date). None when the
    text isn't a travel declaration or the date can't be parsed."""
    m = _TRAVEL_RE.search(text.strip())
    if not m:
        return None
    today = today or date.today()
    end = _parse_fuzzy_date(m.group(2).strip().rstrip(".!?"), today)
    if end is None or end < today:
        return None
    return today, end


def _parse_fuzzy_date(s: str, today: date) -> date | None:
    s = s.lower().strip()
    s = re.sub(r"^(the|next)\s+", "", s)
    # "26th" / "26"
    m = re.fullmatch(r"(\d{1,2})(st|nd|rd|th)?", s)
    if m:
        day = int(m.group(1))
        try:
            cand = today.replace(day=day)
        except ValueError:
            return None
        if cand < today:
            nxt_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
            try:
                cand = nxt_month.replace(day=day)
            except ValueError:
                return None
        return cand
    # "7/26" or "7-26"
    m = re.fullmatch(r"(\d{1,2})[/\-](\d{1,2})", s)
    if m:
        try:
            cand = date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        return cand if cand >= today else cand.replace(year=today.year + 1)
    # "july 26" / "26 july"
    m = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(st|nd|rd|th)?", s)
    if m and m.group(1) in _MONTHS:
        try:
            cand = date(today.year, _MONTHS[m.group(1)], int(m.group(2)))
        except ValueError:
            return None
        return cand if cand >= today else cand.replace(year=today.year + 1)
    m = re.fullmatch(r"(\d{1,2})(st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)", s)
    if m and m.group(3) in _MONTHS:
        try:
            cand = date(today.year, _MONTHS[m.group(3)], int(m.group(1)))
        except ValueError:
            return None
        return cand if cand >= today else cand.replace(year=today.year + 1)
    # weekday name → the next one
    if s in _WEEKDAYS:
        delta = (_WEEKDAYS[s] - today.weekday()) % 7 or 7
        return today + timedelta(days=delta)
    return None
