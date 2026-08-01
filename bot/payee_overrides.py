"""Payee → category rules, stored in the ``auto_rule`` table.

Why rules exist: the LLM categorizer is restricted to spending-only
categories (Day to Day Expenses + Reimbursables, ~14 of the 69 categories).
Recurring bills live in the named "(Nth)" envelopes that are deliberately
excluded from auto-suggestion. So an AT&T charge would never get "Cell Phone
(4th)" from the LLM — it'd get the least-wrong spending category instead
(commonly Transportation, by free association with AT&T Mobility).

Rules catch those bills BEFORE the LLM runs. Each row in ``auto_rule`` is a
regex matched case-insensitively against the raw payee text; the first
enabled rule by ascending id wins.

Until 2026-08-01 these lived in a hard-coded OVERRIDES list in this module,
which meant changing one bill's category needed a code edit and a bot
restart. They now live in the DB and are managed from the Auto-Sync panel;
scripts/archive/seed_auto_rules.py migrated the original 36 entries across.

A matching rule files the charge and sends an FYI rather than a question —
see bot/dispatch.py, which owns that decision.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from bot import storage

log = logging.getLogger(__name__)


def match_rule(db_path: Path | str, payee: str | None) -> dict | None:
    """First enabled auto_rule whose pattern matches ``payee``.

    Match order is ascending id — the same order list_auto_rules returns.
    A rule with an uncompilable pattern is logged and skipped rather than
    raised: one malformed row must not take down categorization for every
    transaction behind it.
    """
    if not payee:
        return None
    for rule in storage.list_auto_rules(db_path, enabled_only=True):
        try:
            pat = re.compile(rule["pattern"], re.I)
        except re.error as e:
            log.warning("auto_rule %s has an invalid pattern %r: %s",
                        rule["id"], rule["pattern"], e)
            continue
        if pat.search(payee):
            return {
                "rule_id": rule["id"],
                "category_id": rule["category_id"],
                "pattern": rule["pattern"],
            }
    return None


def resolve_payee_override(db_path: Path | str, payee: str) -> dict | None:
    """Back-compatible wrapper: returns the matched rule's category with
    its display name, or None. Existing callers in bot/ingest.py and
    bot/suggest.py use this shape.
    """
    m = match_rule(db_path, payee)
    if m is None:
        return None
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT name FROM category WHERE id = ? AND hidden = 0",
            (m["category_id"],),
        ).fetchone()
    if row is None:
        log.warning("auto_rule %s points at missing/hidden category %s",
                    m["rule_id"], m["category_id"])
        return None
    return {
        "category_id": m["category_id"],
        "category_name": row["name"],
        "source": "payee_override",
        "rule": m["pattern"],
    }
