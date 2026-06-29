"""Hard-coded payee → category overrides for recurring bills.

Why: the LLM categorizer is restricted to spending-only categories (Day
to Day Expenses + Reimbursables, ~14 of the 69 YNAB categories). Recurring
bills live in the named "(Nth)" envelopes that are deliberately excluded
from auto-suggestion. So an AT&T charge would never get "Cell Phone (4th)"
from the LLM — it'd get the least-wrong spending category instead
(commonly Transportation, by free association with AT&T Mobility).

This override map catches those bills BEFORE the LLM runs. Each entry is
a regex against the raw payee text. First match wins; resolves the name
to an actual category id via DB lookup.

Add a new entry by appending to OVERRIDES. Names must match category
table verbatim (case-insensitive, but exact otherwise).

The list is intentionally conservative: only payees Steven actually has
in his statement history. Don't speculate. Wrong override is worse than
no override — the LLM might have picked a better fallback.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple

from bot import storage

log = logging.getLogger(__name__)


class _Rule(NamedTuple):
    pattern: re.Pattern
    category_name: str


# Pattern matches against the raw payee string (case-insensitive). Use
# anchored patterns (\b...\b) when the name might overlap other merchants
# (e.g. "Hello Fresh" shouldn't catch "Hello Kitty Store").
OVERRIDES: list[_Rule] = [
    # --- Communications ---
    # \s* (not \s+) on the multi-word patterns so merchant statements that
    # smash the words together (DUKEENERGY, MASSMUTUAL, HOLLYSPRINGS*)
    # still match. CC alerts often strip the space between corporate names.
    _Rule(re.compile(r"\bAT\s*&\s*T\b|\bATT\b|\bAT&T\b", re.I), "Cell Phone (4th)"),
    _Rule(re.compile(r"\bVERIZON\b|\bVZN\b|\bVZW\b", re.I), "Cell Phone (4th)"),
    _Rule(re.compile(r"\bT-?MOBILE\b", re.I), "Cell Phone (4th)"),
    _Rule(re.compile(r"\bSPECTRUM\b|\bCHARTER\s*COMM", re.I), "Internet (12th)"),
    _Rule(re.compile(r"\bCOMCAST\b|\bXFINITY\b", re.I), "Internet (12th)"),

    # --- Utilities ---
    _Rule(re.compile(r"\bDUKE\s*ENERGY\b|\bDUKE-ENERGY\b|\bDUKEENERGY\b", re.I), "Electric (24th)"),
    # Dominion is the natural-GAS utility here (Duke is electric). History:
    # 61 Dominion charges categorized as Gas (20th).
    _Rule(re.compile(r"\bDOMINION\s*ENERGY\b|\bDOMINIONENERGY\b", re.I), "Gas (20th)"),
    _Rule(re.compile(r"\bCITY\s*OF\s*APEX\b|\bAPEX\s*UTILITIES\b", re.I), "Water and Trash (16th)"),
    _Rule(re.compile(r"\bHOLLY\s*SPRINGS?\s*\*?\s*UTILITIES?\b|\bHOLLYSPRINGS\b", re.I), "Water and Trash (16th)"),
    _Rule(re.compile(r"\bTOWN\s*OF\s*HOLLY\s*SPRINGS?\b", re.I), "Water and Trash (16th)"),

    # --- Streaming subscriptions (named envelopes) ---
    _Rule(re.compile(r"\bAUDIBLE\b", re.I), "Audible (30th)"),
    _Rule(re.compile(r"\bHULU\b|\bHLU\*", re.I), "Hulu (7th)"),
    _Rule(re.compile(r"\bNETFLIX\b", re.I), "Netflix (22nd)"),
    _Rule(re.compile(r"\bNINTENDO\b", re.I), "Nintendo Online"),
    _Rule(re.compile(r"\bPLAYSTATION\b|\bSONY\s+PLAYSTATION\b", re.I), "Playstation (26th)"),

    # --- Recurring services ---
    _Rule(re.compile(r"\bAMAZON\s+PRIME\b", re.I), "Amazon Prime"),
    _Rule(re.compile(r"\bHELLO\s*FRESH\b|\bHELLOFRESH\b", re.I), "Hello Fresh"),
    _Rule(re.compile(r"\b1PASSWORD\b|\bAGILEBITS\b", re.I), "Password Manager"),
    _Rule(re.compile(r"\bAPPEST\b|\bTICKTICK\b", re.I), "Appest TickTick"),
    _Rule(re.compile(r"\bKEEPER\s*SECURITY\b|\bKEEPER\s+SEC\b", re.I), "Keeper Renewal"),
    _Rule(re.compile(r"\bYNAB\b|\bYOU\s+NEED\s+A\s+BUDGET\b", re.I), "YNAB renewal"),

    # --- Insurance ---
    _Rule(re.compile(r"\bMASS\s*MUTUAL\b|\bMASSMUTUAL\b", re.I), "Mass Mutual Insurances (17th)"),
    _Rule(re.compile(r"\bAMICA\b", re.I), "Amica Car Insurance (27th)"),

    # --- Investments / Savings Transfers (is_spending=0; bot-local) ---
    # Money flowing to these destinations is SAVING, not spending. The
    # categories live in the bot-local "Investments / Savings Transfers"
    # group seeded by scripts/bootstrap_investment_categories.py and are
    # not pushed to YNAB.
    _Rule(re.compile(r"\bMARCUS\b|\bGOLDMAN\s*SACHS\s*BANK\b", re.I), "Marcus Online Bank"),
    _Rule(re.compile(r"\bCOASTAL\s*FCU\b|\bCOASTAL\s*FEDERAL\s*CREDIT\b", re.I), "Coastal Federal Credit Union"),
    _Rule(re.compile(r"\bTREASURY\s*DIRECT\b|\bTREAS\s*DIR\b|\bTREASURYDIRECT\b", re.I), "Treasury Direct (T-Bills)"),
    _Rule(re.compile(r"\bCOINBASE\b", re.I), "Coinbase"),
    _Rule(re.compile(r"\bVANGUARD\b|\bVGI\b", re.I), "Vanguard - Steven Roth IRA"),
    _Rule(re.compile(r"\bFIDELITY\s+(?:NETBEN|INV|INVEST)\b|\bFIDELITY\s+401K\b", re.I), "Fidelity 401K"),
    _Rule(re.compile(r"\bPRINCIPAL\s*FINANCIAL\b|\bPRINCIPAL\s*GRP\b", re.I), "Principal Financial 401K"),
    _Rule(re.compile(r"\bGUIDELINE\b", re.I), "Guideline 401K"),
    _Rule(re.compile(r"\bAMERICAN\s+FUNDS?\b", re.I), "American Funds SIMPLE IRA"),
    _Rule(re.compile(r"\bSCHWAB\b|\bCHARLES\s+SCHWAB\b", re.I), "Schwab Stock Account"),
    _Rule(re.compile(r"\bTD\s*AMERITRADE\b", re.I), "TD Ameritrade Stock"),
    _Rule(re.compile(r"\bHEALTH\s*EQUITY\b|\bHEALTHEQUITY\b", re.I), "Health Equity HSA"),
    _Rule(re.compile(r"\bNATIONWIDE\b", re.I), "Nationwide 401K"),
]


def resolve_payee_override(
    db_path: Path | str, payee: str,
) -> dict | None:
    """Return {category_id, category_name} for a payee that matches an
    OVERRIDE, or None if no rule fires.

    Looks up the category id from the local `category` table by exact
    case-insensitive name match. Hidden + closed categories are excluded.
    """
    if not payee:
        return None
    for rule in OVERRIDES:
        if not rule.pattern.search(payee):
            continue
        with storage.connect(db_path) as con:
            row = con.execute(
                "SELECT id, name FROM category "
                "WHERE LOWER(name) = LOWER(?) AND hidden = 0",
                (rule.category_name,),
            ).fetchone()
        if row is None:
            log.warning(
                "payee override matched %r → %r but category not found in DB; "
                "did the name change?",
                payee, rule.category_name,
            )
            continue
        return {
            "category_id": row["id"],
            "category_name": row["name"],
            "source": "payee_override",
            "rule": rule.pattern.pattern,
        }
    return None
