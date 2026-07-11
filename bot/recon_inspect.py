"""Read-only inspection reports for the Tauri reconciler-troubleshooting tab.

The bot already does matching (matcher.py + matched_charge), dedupe
(ingest.py via dedupe_key / ledger_signal) and balance reconciliation
(reconciler.py) silently in the background. This module exposes the *state*
of all three so the operator can see, in the UI, what linked up and what
didn't — and crucially *why* — without touching a single row.

Three reports, all pure reads:

  matching_report  — order ↔ YNAB-charge matching (matcher.py). Shows
                     confirmed matches with their score, unmatched orders
                     with their best near-miss candidates + score
                     breakdown, and YNAB charges that look like retail but
                     never got linked to an order.

  dedupe_report    — email-alert vs ynab_sync collapse (ingest dedupe).
                     Shows ledger rows confirmed from both sides (merged),
                     email-only rows still waiting for a YNAB confirmation,
                     and — the real bug signal — distinct ledger rows that
                     share a dedupe_key and should have merged but didn't.

  balance_report   — per-account ledger total vs bank-observed balance
                     (reconciler.reconcile_preview), newest observation per
                     account, biggest drift first.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from bot import matcher, reconciler, storage

# Sources the matcher knows how to score. Mirrors matcher._PAYEE_PATTERNS.
_MATCHABLE_SOURCES = ("amazon", "venmo", "apple")

# How far back to scan the ledger for the "unmatched YNAB charge" and
# dedupe views. Matching/dedupe problems are recent-data problems; a wide
# window just bloats the payload with ancient settled rows.
_LOOKBACK_DAYS = 180
_DEDUPE_LOOKBACK_DAYS = 120

# Payload caps so a pathological DB never ships a 50k-row blob to the UI.
_CAP_MATCHED = 250
_CAP_UNMATCHED_ORDERS = 250
_CAP_UNMATCHED_TXNS = 200
_CAP_CANDIDATES = 4


def _d(value: Any) -> date | None:
    """Coerce a SQLite date/datetime string (or date) to a date."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _order_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source": row["source"],
        "external_id": row.get("external_id"),
        "order_date": str(row["order_date"])[:10] if row.get("order_date") else None,
        "total_cents": row["total_cents"],
        "status": row.get("status"),
        "suggested_category": row.get("suggested_category"),
        "chosen_category": row.get("chosen_category"),
        "summary": (row.get("raw_summary") or "")[:200],
    }


def _txn_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ynab_txn_id": row.get("ynab_txn_id"),
        "account_id": row.get("account_id"),
        "posted_date": str(row["posted_date"])[:10] if row.get("posted_date") else None,
        "amount_cents": row["amount_cents"],
        "payee": row.get("payee"),
        "memo": row.get("memo"),
        "category_id": row.get("category_id"),
        "cleared": row.get("cleared"),
    }


def _order_for_score(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_date": _d(row["order_date"]),
        "total_cents": row["total_cents"],
        "external_id": row.get("external_id"),
    }


def _txn_for_score(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "txn_date": _d(row["posted_date"]),
        "amount_cents": row["amount_cents"],
        "payee": row.get("payee") or "",
        "memo": row.get("memo") or "",
    }


def _detect_source(payee: str | None) -> str | None:
    """First matchable source whose payee regex hits, or None."""
    if not payee:
        return None
    for src in _MATCHABLE_SOURCES:
        pattern = matcher._PAYEE_PATTERNS.get(src)
        if pattern and pattern.search(payee):
            return src
    return None


# ────────────────────────────────────────────────────────────────────────────
# 1. Order ↔ charge matching
# ────────────────────────────────────────────────────────────────────────────


def matching_report(db_path: Path | str) -> dict[str, Any]:
    since = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()

    with storage.connect(db_path) as con:
        matched_rows = con.execute(
            """SELECT mc.pending_order_id, mc.ynab_txn_id, mc.matched_at,
                      po.id AS po_id, po.source, po.external_id, po.order_date,
                      po.total_cents, po.status, po.suggested_category,
                      po.chosen_category, po.raw_summary
               FROM matched_charge mc
               JOIN pending_order po ON po.id = mc.pending_order_id
               ORDER BY mc.matched_at DESC
               LIMIT ?""",
            (_CAP_MATCHED,),
        ).fetchall()
        matched_rows = [dict(r) for r in matched_rows]

        # Unmatched orders: categorized OR still pending, never linked.
        unmatched_order_rows = con.execute(
            f"""SELECT po.* FROM pending_order po
                LEFT JOIN matched_charge mc ON mc.pending_order_id = po.id
                WHERE po.source IN ({",".join("?" * len(_MATCHABLE_SOURCES))})
                  AND po.status IN ('pending', 'categorized')
                  AND mc.pending_order_id IS NULL
                ORDER BY po.order_date DESC
                LIMIT ?""",
            (*_MATCHABLE_SOURCES, _CAP_UNMATCHED_ORDERS),
        ).fetchall()
        unmatched_order_rows = [dict(r) for r in unmatched_order_rows]

        # Recent one-row-per-charge ledger txns, for the YNAB side + as the
        # candidate pool when scoring unmatched orders.
        ledger_rows = con.execute(
            """SELECT id, account_id, posted_date, amount_cents, payee, memo,
                      category_id, cleared, ynab_txn_id
               FROM ledger_txn
               WHERE posted_date >= ?
                 AND parent_txn_id IS NULL
                 AND is_split = 0
               ORDER BY posted_date DESC""",
            (since,),
        ).fetchall()
        ledger_rows = [dict(r) for r in ledger_rows]

        matched_ynab_ids = {
            r["ynab_txn_id"] for r in con.execute(
                "SELECT DISTINCT ynab_txn_id FROM matched_charge"
            ).fetchall()
        }

        # Resolve each matched charge's txn details (ledger first, then
        # pending_txn) so the row shows what it linked to.
        def _resolve_txn(yid: str) -> dict[str, Any] | None:
            lr = con.execute(
                """SELECT id, account_id, posted_date, amount_cents, payee,
                          memo, category_id, cleared, ynab_txn_id
                   FROM ledger_txn WHERE ynab_txn_id = ? LIMIT 1""",
                (yid,),
            ).fetchone()
            if lr:
                return dict(lr)
            pt = con.execute(
                """SELECT id, ynab_txn_id, ynab_account_id AS account_id,
                          txn_date AS posted_date, amount_cents, payee, memo
                   FROM pending_txn WHERE ynab_txn_id = ? LIMIT 1""",
                (yid,),
            ).fetchone()
            return dict(pt) if pt else None

        matched = []
        for r in matched_rows:
            txn_row = _resolve_txn(r["ynab_txn_id"])
            order = {
                "id": r["po_id"], "source": r["source"],
                "external_id": r["external_id"], "order_date": r["order_date"],
                "total_cents": r["total_cents"], "status": r["status"],
                "suggested_category": r["suggested_category"],
                "chosen_category": r["chosen_category"],
                "raw_summary": r["raw_summary"],
            }
            score = None
            if txn_row and _d(order["order_date"]) and _d(txn_row["posted_date"]):
                score = matcher.score_breakdown(
                    _order_for_score(order), _txn_for_score(txn_row),
                    source=r["source"],
                )
            matched.append({
                "order": _order_view(order),
                "txn": _txn_view(txn_row) if txn_row else None,
                "ynab_txn_id": r["ynab_txn_id"],
                "matched_at": str(r["matched_at"]) if r["matched_at"] else None,
                "score": score,
            })

    # Candidate scoring for unmatched orders, against the recent ledger pool.
    unmatched_orders = []
    for o in unmatched_order_rows:
        o_date = _d(o["order_date"])
        if o_date is None:
            unmatched_orders.append({"order": _order_view(o), "candidates": []})
            continue
        win_lo = o_date
        win_hi = o_date + timedelta(days=matcher._DATE_WINDOW_DAYS)
        os = _order_for_score(o)
        scored = []
        for t in ledger_rows:
            t_date = _d(t["posted_date"])
            if t_date is None or not (win_lo <= t_date <= win_hi):
                continue
            bd = matcher.score_breakdown(os, _txn_for_score(t), source=o["source"])
            if bd["total"] <= 0:
                continue
            scored.append((bd["total"], t, bd))
        scored.sort(key=lambda x: x[0], reverse=True)
        unmatched_orders.append({
            "order": _order_view(o),
            "candidates": [
                {"txn": _txn_view(t), "score": bd}
                for _, t, bd in scored[:_CAP_CANDIDATES]
            ],
        })

    # YNAB-side charges that look like retail (payee matches a pattern) but
    # are not linked to any order.
    unmatched_txns = []
    for t in ledger_rows:
        if t.get("ynab_txn_id") and t["ynab_txn_id"] in matched_ynab_ids:
            continue
        src = _detect_source(t.get("payee"))
        if src is None:
            continue
        t_date = _d(t["posted_date"])
        best = None
        if t_date is not None:
            ts = _txn_for_score(t)
            cand = []
            for o in unmatched_order_rows:
                if o["source"] != src:
                    continue
                o_date = _d(o["order_date"])
                if o_date is None:
                    continue
                if not (o_date <= t_date <= o_date + timedelta(days=matcher._DATE_WINDOW_DAYS)):
                    continue
                bd = matcher.score_breakdown(_order_for_score(o), ts, source=src)
                if bd["total"] > 0:
                    cand.append((bd["total"], o, bd))
            cand.sort(key=lambda x: x[0], reverse=True)
            if cand:
                _, o, bd = cand[0]
                best = {"order": _order_view(o), "score": bd}
        unmatched_txns.append({
            "txn": _txn_view(t), "detected_source": src, "best_candidate": best,
        })
        if len(unmatched_txns) >= _CAP_UNMATCHED_TXNS:
            break

    return {
        "threshold": 0.85,
        "ambiguity_gap": 0.10,
        "weights": {
            "amount": matcher._W_AMOUNT, "date": matcher._W_DATE,
            "payee": matcher._W_PAYEE, "memo": matcher._W_MEMO,
        },
        "lookback_days": _LOOKBACK_DAYS,
        "matched": matched,
        "unmatched_orders": unmatched_orders,
        "unmatched_txns": unmatched_txns,
        "counts": {
            "matched": len(matched),
            "unmatched_orders": len(unmatched_orders),
            "unmatched_txns": len(unmatched_txns),
        },
    }


# ────────────────────────────────────────────────────────────────────────────
# 1b. Ledger ↔ live YNAB reconciliation (the three-column diff)
# ────────────────────────────────────────────────────────────────────────────

_RECONCILE_DEFAULT_DAYS = 90
_FUZZY_DATE_WINDOW = 2  # days


def _account_maps(con) -> dict[str, Any]:
    rows = con.execute(
        "SELECT id, name, type, ynab_account_id FROM account"
    ).fetchall()
    name_by_id, name_by_ynab, ynab_by_id = {}, {}, {}
    for r in rows:
        name_by_id[r["id"]] = r["name"]
        ynab_by_id[r["id"]] = r["ynab_account_id"]
        if r["ynab_account_id"]:
            name_by_ynab[r["ynab_account_id"]] = r["name"]
    return {
        "name_by_id": name_by_id,
        "name_by_ynab": name_by_ynab,
        "ynab_by_id": ynab_by_id,
    }


def _ledger_item(row: dict[str, Any], name_by_id: dict[str, str]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ynab_txn_id": row.get("ynab_txn_id"),
        "date": str(row["posted_date"])[:10] if row.get("posted_date") else None,
        "payee": row.get("payee") or "",
        "amount_cents": row["amount_cents"],
        "account": name_by_id.get(row.get("account_id"), "—"),
        "cleared": row.get("cleared"),
        "source": row.get("source_signal"),
    }


def _ynab_item(t: dict[str, Any], name_by_ynab: dict[str, str]) -> dict[str, Any]:
    d = t.get("txn_date")
    return {
        "ynab_txn_id": t["ynab_txn_id"],
        "date": str(d)[:10] if d else None,
        "payee": t.get("payee") or "",
        "amount_cents": t["amount_cents"],
        "account": name_by_ynab.get(t.get("ynab_account_id"), "—"),
        "cleared": t.get("cleared"),
        "approved": t.get("approved"),
    }


def ledger_vs_ynab_report(
    db_path: Path | str,
    settings: Any,
    *,
    days: int = _RECONCILE_DEFAULT_DAYS,
) -> dict[str, Any]:
    """Diff our local ledger against the live YNAB ledger.

    Three buckets keyed first by YNAB transaction id, then a soft second
    pass that pairs leftover rows with the same account + amount within a
    couple of days (same real charge, just never linked):

      matched     — present on both sides
      ledger_only — we have it, YNAB doesn't
      ynab_only   — YNAB has it, we don't
    """
    from bot.ynab_client import YnabClient

    token = getattr(settings, "ynab_token", "")
    if not token:
        raise RuntimeError("YNAB token not configured")

    since = date.today() - timedelta(days=days)
    client = YnabClient(token, settings.ynab.budget_id)
    ynab_txns = client.list_all_transactions(since=since)

    with storage.connect(db_path) as con:
        amaps = _account_maps(con)
        ledger_rows = [
            dict(r) for r in con.execute(
                """SELECT id, account_id, posted_date, amount_cents, payee,
                          memo, cleared, source_signal, ynab_txn_id
                   FROM ledger_txn
                   WHERE posted_date >= ?
                     AND parent_txn_id IS NULL
                     AND is_split = 0
                   ORDER BY posted_date DESC""",
                (since.isoformat(),),
            ).fetchall()
        ]

    name_by_id = amaps["name_by_id"]
    name_by_ynab = amaps["name_by_ynab"]
    ynab_acct_by_internal = amaps["ynab_by_id"]

    ynab_by_id = {t["ynab_txn_id"]: t for t in ynab_txns}
    used_ynab: set[str] = set()
    matched: list[dict[str, Any]] = []
    ledger_orphans: list[dict[str, Any]] = []

    # Pass 1 — identity match on ynab_txn_id.
    for row in ledger_rows:
        yid = row.get("ynab_txn_id")
        if yid and yid in ynab_by_id:
            t = ynab_by_id[yid]
            used_ynab.add(yid)
            li = _ledger_item(row, name_by_id)
            yi = _ynab_item(t, name_by_ynab)
            matched.append({
                "ledger": li, "ynab": yi, "link": "id",
                "amount_match": li["amount_cents"] == yi["amount_cents"],
                "date_match": li["date"] == yi["date"],
            })
        else:
            ledger_orphans.append(row)

    # Pass 2 — fuzzy pair leftovers: same account + |amount| + date ±2d.
    fuzzy_index: dict[tuple, list[dict[str, Any]]] = {}
    for yid, t in ynab_by_id.items():
        if yid in used_ynab:
            continue
        key = (t.get("ynab_account_id"), abs(int(t["amount_cents"])))
        fuzzy_index.setdefault(key, []).append(t)

    true_ledger_only: list[dict[str, Any]] = []
    for row in ledger_orphans:
        l_date = _d(row["posted_date"])
        l_ynab_acct = ynab_acct_by_internal.get(row.get("account_id"))
        key = (l_ynab_acct, abs(int(row["amount_cents"])))
        candidates = fuzzy_index.get(key, [])
        best = None
        if l_date is not None:
            for t in candidates:
                if t["ynab_txn_id"] in used_ynab:
                    continue
                t_date = _d(t.get("txn_date"))
                if t_date is None:
                    continue
                if abs((t_date - l_date).days) <= _FUZZY_DATE_WINDOW:
                    best = t
                    break
        if best is not None:
            used_ynab.add(best["ynab_txn_id"])
            li = _ledger_item(row, name_by_id)
            yi = _ynab_item(best, name_by_ynab)
            matched.append({
                "ledger": li, "ynab": yi, "link": "fuzzy",
                "amount_match": li["amount_cents"] == yi["amount_cents"],
                "date_match": li["date"] == yi["date"],
            })
        else:
            true_ledger_only.append(_ledger_item(row, name_by_id))

    ynab_only = [
        _ynab_item(t, name_by_ynab)
        for yid, t in ynab_by_id.items()
        if yid not in used_ynab
    ]

    # Flag ledger-only rows that are really DUPLICATES of an already-matched
    # charge (the YNAB copy is matched to a different ledger row). These must
    # NOT be pushed to YNAB — YNAB already has them. A row is a likely dup if
    # a matched row on the same account lines up by exact amount (±10d) or by
    # tip/FX drift (settled up to +40%, ±7d).
    from bot.ynab_full_sync import _payee_tokens
    for lo in true_ledger_only:
        od = _d(lo["date"])
        amt = abs(int(lo["amount_cents"]))
        lo_tokens = _payee_tokens(lo["payee"])
        note = None
        for m in matched:
            ml = m["ledger"]
            if ml["account"] != lo["account"]:
                continue
            md = _d(ml["date"])
            if md is None or od is None:
                continue
            ddays = abs((md - od).days)
            mamt = abs(int(ml["amount_cents"]))
            # Require a shared merchant token so we don't flag two unrelated
            # same-ish-amount charges as duplicates (a $50 Enterprise is NOT
            # a $64 YMCA). Mirrors the merge script's safety check.
            overlap = bool(lo_tokens & (
                _payee_tokens(ml["payee"]) | _payee_tokens(m["ynab"]["payee"])))
            if not overlap:
                continue
            if mamt == amt and ddays <= 10:
                note = f"already in YNAB as {ml['payee']} (±{ddays}d)"
                break
            if lo["amount_cents"] < 0 and amt <= mamt <= int(amt * 1.4) and ddays <= 7:
                extra = (mamt - amt) / 100
                note = (f"already in YNAB as {ml['payee']} "
                        f"(+${extra:.2f} tip/FX, ±{ddays}d)")
                break
        lo["likely_duplicate"] = note is not None
        lo["duplicate_note"] = note

    matched.sort(key=lambda m: m["ledger"]["date"] or "", reverse=True)
    true_ledger_only.sort(
        key=lambda i: (not i.get("likely_duplicate"), i["date"] or ""),
        reverse=True,
    )
    ynab_only.sort(key=lambda i: i["date"] or "", reverse=True)

    discrepancies = sum(
        1 for m in matched if not (m["amount_match"] and m["date_match"])
    )
    likely_dupes = sum(1 for i in true_ledger_only if i.get("likely_duplicate"))

    return {
        "window_days": days,
        "since": since.isoformat(),
        "matched": matched,
        "ledger_only": true_ledger_only,
        "ynab_only": ynab_only,
        "counts": {
            "matched": len(matched),
            "ledger_only": len(true_ledger_only),
            "ynab_only": len(ynab_only),
            "discrepancies": discrepancies,
            "likely_duplicates": likely_dupes,
        },
    }


# ────────────────────────────────────────────────────────────────────────────
# 2. Email-alert ↔ ynab_sync dedupe
# ────────────────────────────────────────────────────────────────────────────


def dedupe_report(db_path: Path | str) -> dict[str, Any]:
    since = (date.today() - timedelta(days=_DEDUPE_LOOKBACK_DAYS)).isoformat()

    with storage.connect(db_path) as con:
        rows = con.execute(
            """SELECT id, account_id, posted_date, amount_cents, payee, memo,
                      category_id, cleared, source_signal, ynab_txn_id,
                      dedupe_key
               FROM ledger_txn
               WHERE posted_date >= ?
                 AND parent_txn_id IS NULL
                 AND is_split = 0
               ORDER BY posted_date DESC""",
            (since,),
        ).fetchall()
        rows = [dict(r) for r in rows]

        sig_rows = con.execute(
            """SELECT ls.ledger_txn_id, ls.signal_kind, ls.email_id,
                      ls.received_at
               FROM ledger_signal ls
               JOIN ledger_txn lt ON lt.id = ls.ledger_txn_id
               WHERE lt.posted_date >= ?
               ORDER BY ls.received_at""",
            (since,),
        ).fetchall()

    signals_by_txn: dict[int, list[dict[str, Any]]] = {}
    for s in sig_rows:
        side = "ynab" if s["signal_kind"] == "ynab_sync" else "email"
        signals_by_txn.setdefault(s["ledger_txn_id"], []).append({
            "signal_kind": s["signal_kind"],
            "email_id": s["email_id"],
            "received_at": str(s["received_at"]) if s["received_at"] else None,
            "side": side,
        })

    merged: list[dict[str, Any]] = []
    email_only: list[dict[str, Any]] = []

    for r in rows:
        sigs = signals_by_txn.get(r["id"], [])
        sides = {s["side"] for s in sigs}
        has_ynab = "ynab" in sides or bool(r.get("ynab_txn_id"))
        has_email = "email" in sides
        view = {
            "txn": _txn_view(r),
            "source_signal": r.get("source_signal"),
            "dedupe_key": r.get("dedupe_key"),
            "signals": sigs,
            "n_email": sum(1 for s in sigs if s["side"] == "email"),
            "n_ynab": sum(1 for s in sigs if s["side"] == "ynab"),
        }
        if has_email and has_ynab:
            merged.append(view)
        elif has_email and not has_ynab:
            email_only.append(view)
        # ynab-only rows are the normal steady state (every YNAB txn) — not
        # interesting for dedupe troubleshooting, so we drop them.

    # Ledger rows sharing a dedupe_key (account|date|amount) that are
    # genuinely the SAME charge represented twice — these should have merged.
    #
    # CRITICAL: a shared dedupe_key alone is NOT a duplicate. account|date|
    # amount is not a unique transaction identity — two *distinct* YNAB
    # transactions legitimately share it (recurring 529 ACH x2 for two kids,
    # two ATM withdrawals same day, a family's airfare booked together). Each
    # of those carries its own distinct real YNAB UUID, and collapsing them
    # would DELETE real transactions and break balances.
    #
    # A real duplicate is redundancy: the rows do NOT each have an independent
    # YNAB identity — e.g. an un-adopted CC-alert / history orphan
    # (ynab_txn_id NULL or 'ledger:N') sharing the key with the synced copy.
    # So we flag a group only when its count of distinct real UUIDs is fewer
    # than its row count (i.e. at least one row lacks its own YNAB txn).
    def _real_uuid(r: dict[str, Any]) -> str | None:
        yid = r.get("ynab_txn_id")
        if not yid or str(yid).startswith("ledger:"):
            return None
        return str(yid)

    by_key: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        k = r.get("dedupe_key")
        if not k:
            continue
        by_key.setdefault(k, []).append(r)
    duplicate_keys = [
        {
            "dedupe_key": k,
            "rows": [
                {**_txn_view(r), "source_signal": r.get("source_signal")}
                for r in grp
            ],
        }
        for k, grp in by_key.items()
        if len(grp) > 1
        and len({u for r in grp if (u := _real_uuid(r)) is not None}) < len(grp)
    ]
    duplicate_keys.sort(key=lambda d: len(d["rows"]), reverse=True)

    return {
        "lookback_days": _DEDUPE_LOOKBACK_DAYS,
        "merged": merged,
        "email_only": email_only,
        "duplicate_keys": duplicate_keys,
        "counts": {
            "merged": len(merged),
            "email_only": len(email_only),
            "duplicate_keys": len(duplicate_keys),
        },
    }


# ────────────────────────────────────────────────────────────────────────────
# 3. Balance reconciliation
# ────────────────────────────────────────────────────────────────────────────


def _ledger_sum_asof(con, account_id: str, as_of) -> int:
    """Our ledger's running balance: every row up to and including as_of.
    is_split = 0 counts split children (which sum to the parent), never
    both."""
    row = con.execute(
        """SELECT COALESCE(SUM(amount_cents), 0) AS s
           FROM ledger_txn
           WHERE account_id = ? AND is_split = 0 AND posted_date <= ?""",
        (account_id, as_of),
    ).fetchone()
    return int(row["s"] or 0)


def balance_report(db_path: Path | str) -> dict[str, Any]:
    """Per-account OUR-LEDGER vs bank-observed balance.

    Steven, 2026-07-11: the comparison that matters is whether the bot's
    ledger matches the bank's daily-balance emails — YNAB's balance is on
    its way out of the picture. expected = ledger running total as of the
    observation date; delta = bank − ledger.
    """
    with storage.connect(db_path) as con:
        latest = con.execute(
            """SELECT abo.account_id, MAX(abo.as_of_date) AS as_of_date,
                      a.name, a.type
               FROM account_balance_observed abo
               JOIN account a ON a.id = abo.account_id
               WHERE a.closed = 0
               GROUP BY abo.account_id"""
        ).fetchall()
        latest = [dict(r) for r in latest]

        accounts = []
        for r in latest:
            as_of = _d(r["as_of_date"])
            if as_of is None:
                continue
            observed = con.execute(
                """SELECT balance_cents FROM account_balance_observed
                   WHERE account_id = ? AND as_of_date = ?""",
                (r["account_id"], r["as_of_date"]),
            ).fetchone()["balance_cents"]
            expected = _ledger_sum_asof(con, r["account_id"], r["as_of_date"])
            delta = int(observed) - expected
            tol = reconciler.DEFAULT_TOLERANCE_CENTS
            accounts.append({
                "account_id": r["account_id"],
                "name": r["name"],
                "type": r["type"],
                "as_of_date": str(r["as_of_date"])[:10],
                "expected_cents": expected,
                "observed_cents": int(observed),
                "delta_cents": delta,
                "status": "ok" if abs(delta) <= tol else "mismatch",
                "would_reconcile_count": 0,
            })

    # Mismatches first, then by absolute drift.
    accounts.sort(
        key=lambda a: (a["status"] != "mismatch", -abs(a["delta_cents"] or 0)),
    )

    return {
        "tolerance_cents": reconciler.DEFAULT_TOLERANCE_CENTS,
        "accounts": accounts,
        "counts": {
            "total": len(accounts),
            "ok": sum(1 for a in accounts if a["status"] == "ok"),
            "mismatch": sum(1 for a in accounts if a["status"] == "mismatch"),
        },
    }


# How far back the day-walk looks. Slips older than this are baseline
# drift; the walk's job is catching the recent ones while the bank email
# trail is still dense.
_WALK_DAYS = 45

# Gap levels are smoothed with a median over this many observations on
# each side of a window. Credit-card balances systematically lag the
# instant charge alerts by 1-2 days (pending vs posted), so raw
# day-window deltas whipsaw; the median absorbs one-observation wobble
# and only a level shift that STICKS reads as a slip.
_SMOOTH_N = 3

# Don't cry slip over drift smaller than this — daily wobble makes tiny
# shifts meaningless even after smoothing. Credit cards get a much higher
# bar: their balance emails lag the instant charge alerts by 1-2 days
# (pending vs posted), so the smoothed gap still wanders by hundreds of
# cents without anything actually slipping.
_SLIP_MIN_CENTS = 500
_SLIP_MIN_CENTS_CC = 2500


def _median(xs: list[int]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return float(s[mid]) if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def balance_walk(db_path: Path | str, days: int = _WALK_DAYS) -> dict[str, Any]:
    """Day-by-day slipped-transaction detector.

    For each pair of consecutive bank balance emails: how much did the
    bank move vs how much did our ledger move? A window where they
    disagree AND the disagreement sticks (doesn't reverse within
    _TIMING_LOOKAHEAD later observations) means a transaction slipped
    through capture — reported with its exact amount.
    """
    tol = reconciler.DEFAULT_TOLERANCE_CENTS
    since = (date.today() - timedelta(days=days)).isoformat()
    with storage.connect(db_path) as con:
        accts = con.execute(
            """SELECT DISTINCT abo.account_id, a.name, a.type
               FROM account_balance_observed abo
               JOIN account a ON a.id = abo.account_id
               WHERE a.closed = 0 AND abo.as_of_date >= ?
               ORDER BY a.name""",
            (since,),
        ).fetchall()

        out = []
        for acct in accts:
            obs = con.execute(
                """SELECT as_of_date, balance_cents
                   FROM account_balance_observed
                   WHERE account_id = ? AND as_of_date >= ?
                   ORDER BY as_of_date""",
                (acct["account_id"], since),
            ).fetchall()
            if len(obs) < 2:
                continue

            # Gap level at each observation: bank − ledger.
            levels = []
            for o in obs:
                led = _ledger_sum_asof(con, acct["account_id"], o["as_of_date"])
                levels.append({
                    "date": str(o["as_of_date"])[:10],
                    "bank_cents": int(o["balance_cents"]),
                    "ledger_cents": led,
                    "gap_cents": int(o["balance_cents"]) - led,
                })

            gaps = [lv["gap_cents"] for lv in levels]
            slip_min = (_SLIP_MIN_CENTS_CC
                        if acct["type"] == "credit_card"
                        else _SLIP_MIN_CENTS)
            windows = []
            for i in range(1, len(levels)):
                prev, cur = levels[i - 1], levels[i]
                gap_change = cur["gap_cents"] - prev["gap_cents"]
                # Smoothed level shift across this window: median of the
                # gap before vs after. One-observation wobble (bank posts
                # a charge a day after the alert) cancels; a shift that
                # sticks survives.
                pre = _median(gaps[max(0, i - _SMOOTH_N):i])
                post = _median(gaps[i:i + _SMOOTH_N])
                shift = post - pre
                verdict = "ok"
                if abs(gap_change) > tol:
                    verdict = ("slip"
                               if abs(shift) > max(tol, slip_min)
                               else "timing")
                windows.append({
                    "from_date": prev["date"],
                    "to_date": cur["date"],
                    "bank_delta_cents": cur["bank_cents"] - prev["bank_cents"],
                    "ledger_delta_cents": (cur["ledger_cents"]
                                           - prev["ledger_cents"]),
                    "gap_change_cents": gap_change,
                    "shift_cents": int(round(shift)),
                    "verdict": verdict,
                })

            slips = [w for w in windows if w["verdict"] == "slip"]
            baseline = _median(gaps[:_SMOOTH_N])
            current = _median(gaps[-_SMOOTH_N:])
            out.append({
                "account_id": acct["account_id"],
                "name": acct["name"],
                "type": acct["type"],
                "levels": levels,
                "windows": windows,
                "slips": slips,
                "baseline_gap_cents": int(round(baseline)),
                "current_gap_cents": int(round(current)),
                "net_drift_cents": int(round(current - baseline)),
            })

    out.sort(key=lambda a: -abs(a["net_drift_cents"]))
    return {
        "tolerance_cents": tol,
        "days": days,
        "accounts": out,
        "counts": {
            "accounts": len(out),
            "slips": sum(len(a["slips"]) for a in out),
        },
    }
