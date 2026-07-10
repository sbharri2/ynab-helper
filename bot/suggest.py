"""Suggestions v2 — deterministic evidence layers (docs/suggestions-v2.md).

This module owns payee normalization and payee-memory v2. It is pure
SQLite + arithmetic: no LLM, no network. ingest._categorize composes the
layers; scripts/eval_suggestions.py replays them against history.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from pathlib import Path

from bot import storage

# Recency half-life for memory votes: a filing 6 months ago counts half
# as much as one today. Keeps memory current as habits shift.
_HALF_LIFE_DAYS = 180.0
# Minimum observations before memory speaks: 1 = YNAB parity ("this is
# what you filed it as last time") for the suggest tier; the strong
# (auto-file) tier keeps its own higher bar below.
_MIN_VOTES = 1
# A history key that is a word-boundary prefix of the query key (or vice
# versa) — usually the same merchant ± a city suffix — votes at reduced
# weight. "TOPGOLF DURHAM DURHAM" must see the 78 plain-"TOPGOLF" votes.
_PREFIX_WEIGHT = 0.6
_PREFIX_MIN_LEN = 5
# Confidence needed for a plain suggestion vs an auto-file-grade prior.
SUGGEST_SHARE = 0.60
STRONG_SHARE = 0.80
STRONG_COUNT = 3

_TRAILING_USA_RE = re.compile(r"\s+USA\s*$", re.I)
_PREFIX_RE = re.compile(r"^(TST\*\s*|SQ\s*\*\s*|SP\s+|PY\s*\*\s*|PAYPAL\s*\*\s*|IN\s*\*\s*)", re.I)
_STORE_NUM_RE = re.compile(r"[#*]\s*\d+")
_LONG_DIGITS_RE = re.compile(r"\d{3,}")
_WS_RE = re.compile(r"\s{2,}")


def normalize_payee(payee: str | None) -> str:
    """Collapse processor prefixes, store numbers and long digit runs so
    'TST* TOPGOLF - DURHAM DURHAM USA' and 'Topgolf' share one memory key
    prefix-wise. City suffixes are NOT stripped here (we can't tell city
    from name reliably); memory instead also indexes a city-stripped key
    for raw CC-alert payees via `city_stripped_key`.
    """
    if not payee:
        return ""
    s = payee.upper().strip()
    s = _TRAILING_USA_RE.sub("", s)
    s = _PREFIX_RE.sub("", s)
    s = _STORE_NUM_RE.sub("", s)
    s = _LONG_DIGITS_RE.sub("", s)
    s = re.sub(r"[/\-]", " ", s)   # CVS/PHARMACY == CVS PHARMACY
    return _WS_RE.sub(" ", s).strip(" -*")


def _key_match_weight(query_key: str, hist_key: str) -> float:
    """1.0 exact, _PREFIX_WEIGHT for a word-boundary prefix relation
    (same merchant ± city suffix), 0.0 otherwise."""
    if query_key == hist_key:
        return 1.0
    short, long_ = ((query_key, hist_key)
                    if len(query_key) <= len(hist_key)
                    else (hist_key, query_key))
    if (len(short) >= _PREFIX_MIN_LEN
            and long_.startswith(short)
            and long_[len(short):len(short) + 1] == " "):
        return _PREFIX_WEIGHT
    return 0.0


def _decay(days_ago: float) -> float:
    return math.pow(0.5, max(0.0, days_ago) / _HALF_LIFE_DAYS)


def _as_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _amount_band(amount_cents: int) -> tuple[int, int]:
    """±max($10, 25%) around the charge — 'the usual amount for this bill
    / a solo meal vs the family one'."""
    a = abs(amount_cents)
    slack = max(1000, int(a * 0.25))
    return a - slack, a + slack


def payee_memory(db_path: Path | str, payee: str, *,
                 as_of: date | None = None,
                 amount_cents: int | None = None) -> dict | None:
    """Recency-weighted majority vote over everything this payee has ever
    been filed as (5y ledger history + human pending filings).

    When ``amount_cents`` is given, a second vote restricted to
    similar-amount observations is computed — payees like AT&T (two
    bills), gas stations (fuel vs snacks) or Chick-fil-A (solo vs family)
    split cleanly by amount where they don't by name.

    Returns None when the payee is unknown / the vote is too thin.
    ``as_of`` limits history to strictly-before that date (backtesting
    without leakage).
    """
    key = normalize_payee(payee)
    if not key or len(key) < 3:
        return None
    ref = as_of or date.today()
    # The payee filter happens in Python because normalization can't be
    # expressed in SQL. Volume (~13k rows) is fine at ingest rates.
    with storage.connect(db_path) as con:
        rows_all = con.execute(
            """SELECT posted_date AS d, category_id AS c, payee,
                      amount_cents AS a
               FROM ledger_txn
               WHERE category_id IS NOT NULL AND is_split = 0
                 AND payee IS NOT NULL""",
        ).fetchall()
        # Pending rows add the freshest signal (chosen but maybe not
        # pushed to the ledger yet). A filed pending row whose category
        # was already promoted to the ledger votes twice — deliberate:
        # explicit human confirmations outweigh imported history.
        prows = con.execute(
            """SELECT pt.txn_date AS d, pt.chosen_category AS c, pt.payee,
                      pt.amount_cents AS a
               FROM pending_txn pt
               WHERE pt.status = 'categorized'
                 AND pt.chosen_category IS NOT NULL
                 AND (pt.filed_by IS NULL OR pt.filed_by NOT LIKE 'auto%')""",
        ).fetchall()

    votes: dict[str, float] = {}
    counts: dict[str, int] = {}
    band_votes: dict[str, float] = {}
    band_counts: dict[str, int] = {}
    band = _amount_band(amount_cents) if amount_cents else None
    for r in list(rows_all) + list(prows):
        kw = _key_match_weight(key, normalize_payee(r["payee"]))
        if kw == 0.0:
            continue
        d = _as_date(r["d"])
        if d is None or (as_of is not None and d >= ref):
            continue
        w = kw * _decay((ref - d).days)
        votes[r["c"]] = votes.get(r["c"], 0.0) + w
        counts[r["c"]] = counts.get(r["c"], 0) + 1
        if band and r["a"] is not None and band[0] <= abs(r["a"]) <= band[1]:
            band_votes[r["c"]] = band_votes.get(r["c"], 0.0) + w
            band_counts[r["c"]] = band_counts.get(r["c"], 0) + 1
    if not votes:
        return None
    n = sum(counts.values())
    if n < _MIN_VOTES:
        return None
    total = sum(votes.values())
    best_cat = max(votes, key=votes.get)
    out = {
        "category_id": best_cat,
        "share": votes[best_cat] / total,
        "weight": votes[best_cat],
        "n": n,
        "n_best": counts.get(best_cat, 0),
        "band_category_id": None,
        "band_share": 0.0,
        "band_n": 0,
        "band_n_best": 0,
    }
    if band_votes:
        btotal = sum(band_votes.values())
        bbest = max(band_votes, key=band_votes.get)
        out.update(band_category_id=bbest,
                   band_share=band_votes[bbest] / btotal,
                   band_n=sum(band_counts.values()),
                   band_n_best=band_counts.get(bbest, 0))
    with storage.connect(db_path) as con:
        nm = con.execute("SELECT name FROM category WHERE id = ?",
                         (out["category_id"],)).fetchone()
    out["category_name"] = nm["name"] if nm else None
    return out


def memory_suggestion(db_path: Path | str, payee: str, *,
                      as_of: date | None = None,
                      amount_cents: int | None = None) -> tuple[str | None, str]:
    """(category_id, tier) where tier ∈ 'strong' (auto-file grade),
    'suggest' (show the human), 'none'.

    Amount-band rules:
      * band agrees with the global winner → tier unchanged (reinforced)
      * band disagrees with confidence (n≥2, share≥0.8) → the band wins,
        but only ever at 'suggest' tier (never auto-file a split payee)
      * global says strong but the band is thin/absent for a payee whose
        history splits by amount → strong stands (unanimous history)
    """
    m = payee_memory(db_path, payee, as_of=as_of, amount_cents=amount_cents)
    if m is None:
        return None, "none"
    cat = m["category_id"]
    if (m["band_category_id"] is not None
            and m["band_category_id"] != cat
            and m["band_n_best"] >= 2 and m["band_share"] >= 0.8):
        return m["band_category_id"], "suggest"
    if m["share"] >= STRONG_SHARE and m["n_best"] >= STRONG_COUNT:
        # Measured 2026-07-10: gating strong on "usual amount" dropped 9
        # auto-files to prevent 1 error — not worth it; the Recently-Filed
        # safety net covers the residual ~10%.
        return cat, "strong"
    if m["share"] >= SUGGEST_SHARE:
        return cat, "suggest"
    return None, "none"
