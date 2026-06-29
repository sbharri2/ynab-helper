"""Phase 7 /batch command — bulk-process COLD-lane pending_txns.

Default UX (since 2026-06-26): default-confirmed checkbox flow.
  1. User types /batch.
  2. Bot sends ONE message with every row as a tappable button starting ☑.
  3. User taps any row to flag it for individual review (☑ → ☐).
  4. User taps "✅ Submit" — checked rows commit with their guesses in one
     transaction; flagged rows are then DM'd one-by-one with the standard
     single-item keyboard for category override.

Fallback (still supported): shorthand text reply for power users:

      all                       confirm every guess
      all except 3=groceries    confirm all, but override 3
      1 2 4 5                   confirm only those numbers
      3=groceries               override item 3 to Groceries
      5 skip                    skip item 5
      7 back                    route item 7 to Allison

The batch DM's pending_txn IDs (and current checkbox states) live in
``bot_conversation.last_batch_json`` so taps and Submit can map row
numbers back to specific rows even minutes after the DM was sent.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot import storage

PAGE_SIZE = 15


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Building the batch DM
# ---------------------------------------------------------------------------

_AMAZON_PAYEE_SQL = (
    "(UPPER(pt.payee) LIKE '%AMAZON%' OR UPPER(pt.payee) LIKE '%AMZN%')"
)


def build_batch(
    db_path: Path | str, *, user_id: str, page_size: int = PAGE_SIZE,
) -> list[dict]:
    """Return up to ``page_size`` pending_txns for `user_id`.

    Per the 2026-06-28 directive, /batch and /pending now show the SAME
    set: every pending row regardless of queue_lane, EXCEPT Amazon (own
    /amazon mechanism) and 'hold' (waiting for receipt enrichment, not
    yet actionable).
    """
    with storage.connect(db_path) as con:
        rows = con.execute(
            f"""SELECT pt.id AS pt_id, pt.payee, pt.amount_cents, pt.txn_date,
                       pt.raw_summary, pt.suggested_category,
                       c.name AS suggested_category_name
                FROM pending_txn pt
                LEFT JOIN category c ON c.id = pt.suggested_category
                WHERE pt.assigned_to_user_id = ?
                  AND pt.status = 'pending'
                  AND pt.queue_lane <> 'hold'
                  AND NOT {_AMAZON_PAYEE_SQL}
                ORDER BY pt.txn_date ASC, pt.id ASC
                LIMIT ?""",
            (user_id, page_size),
        ).fetchall()
    return [dict(r) for r in rows]


def build_amazon_batch(
    db_path: Path | str, *, user_id: str, page_size: int = PAGE_SIZE,
) -> list[dict]:
    """Amazon-only counterpart to build_batch. Returns COLD-lane Amazon
    pending_txns (i.e., enriched with order detail OR aged out of the
    14-day hold). HOLD items waiting for receipts are NOT included — the
    bot can't categorize those yet."""
    with storage.connect(db_path) as con:
        rows = con.execute(
            f"""SELECT pt.id AS pt_id, pt.payee, pt.amount_cents, pt.txn_date,
                       pt.raw_summary, pt.suggested_category,
                       c.name AS suggested_category_name
                FROM pending_txn pt
                LEFT JOIN category c ON c.id = pt.suggested_category
                WHERE pt.assigned_to_user_id = ?
                  AND pt.status = 'pending'
                  AND pt.queue_lane = 'cold'
                  AND {_AMAZON_PAYEE_SQL}
                ORDER BY pt.txn_date ASC, pt.id ASC
                LIMIT ?""",
            (user_id, page_size),
        ).fetchall()
    return [dict(r) for r in rows]


def count_cold_batch(db_path: Path | str, *, user_id: str) -> int:
    """Count pending non-Amazon non-hold items for `user_id` (the
    /batch + /pending unified backlog, per 2026-06-28 directive)."""
    with storage.connect(db_path) as con:
        return con.execute(
            f"""SELECT COUNT(*) FROM pending_txn pt
                WHERE pt.assigned_to_user_id = ?
                  AND pt.status = 'pending'
                  AND pt.queue_lane <> 'hold'
                  AND NOT {_AMAZON_PAYEE_SQL}""",
            (user_id,),
        ).fetchone()[0]


def count_amazon_ready(db_path: Path | str, *, user_id: str) -> int:
    """Count COLD Amazon items ready for /amazon."""
    with storage.connect(db_path) as con:
        return con.execute(
            f"""SELECT COUNT(*) FROM pending_txn pt
                WHERE pt.assigned_to_user_id = ?
                  AND pt.status = 'pending'
                  AND pt.queue_lane = 'cold'
                  AND {_AMAZON_PAYEE_SQL}""",
            (user_id,),
        ).fetchone()[0]


def count_amazon_held(db_path: Path | str, *, user_id: str) -> int:
    """Count HOLD Amazon items (waiting for matching order email)."""
    with storage.connect(db_path) as con:
        return con.execute(
            f"""SELECT COUNT(*) FROM pending_txn pt
                WHERE pt.assigned_to_user_id = ?
                  AND pt.status = 'pending'
                  AND pt.queue_lane = 'hold'
                  AND {_AMAZON_PAYEE_SQL}""",
            (user_id,),
        ).fetchone()[0]


# Generic-brand payees where the merchant-string is uninformative; the
# row's raw_summary almost always has the real merchandise context we'd
# rather show. Kept in sync with bot.ingest._is_generic_payee.
_GENERIC_DISPLAY_PAYEES = (
    "amazon", "amzn", "venmo", "paypal", "apple.com", "apple com", "apl*",
    "amazon mktp", "amazon.com",
)


def _best_display_string(item: dict) -> str:
    """Pick the most informative display string for a row.

    For generic-brand payees (AMAZON MKTPLACE PMTS, APPLE.COM/BILL, etc.)
    the raw_summary carries the real merchandise context; show that.
    For real-merchant payees (HARRIS TEETER, DUKEENERGY), the payee
    string itself is informative — keep showing the payee.
    """
    payee = (item.get("payee") or "")
    payee_lc = payee.lower().strip()
    raw = (item.get("raw_summary") or "").strip()
    if not raw:
        return payee
    # If the payee looks generic, prefer the rich raw_summary.
    if any(payee_lc.startswith(g) for g in _GENERIC_DISPLAY_PAYEES):
        # Strip the redundant "Citi DC ${amount} at <payee> on <date>" prefix
        # that CC-alert raw_summaries default to — it adds no info on top of
        # the columns we already render.
        import re
        cleaned = re.sub(
            r"^(?:Citi|Chase|Coastal)[^$]*\$[\d,]+\.\d{2}\s+at\s+",
            "", raw, flags=re.IGNORECASE,
        )
        # Same idea but for Coastal's "Coastal X $Y type" pattern
        cleaned = re.sub(
            r"^Coastal\s+\w+\s+\$[\d,]+\.\d{2}\s+",
            "", cleaned, flags=re.IGNORECASE,
        )
        if cleaned and cleaned != raw:
            return cleaned[:60]
        return raw[:60]
    return payee


def render_batch_buttons(items: list[dict], payload_items: list[dict]):
    """Build the InlineKeyboardMarkup for a /batch DM.

    Layout: compact numbered tile buttons in rows of 5, each carrying
    just the row number with its ☑/☐ state. The full per-row data lives
    in the message body (rendered by ``render_batch_body``). Tapping any
    tile toggles via ``bt:<n>``; the body re-renders inline. Submit /
    Cancel sit at the bottom.

    Why this shape: Telegram inline buttons are single-line and truncate
    around 30-40 visible characters on mobile. Putting full per-row
    detail in the button text loses the merchant + guess. The body has
    no width limit and can render monospace columns cleanly.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    checked_by_n = {it["n"]: it.get("checked", True) for it in payload_items}
    rows: list[list] = []
    cur: list = []
    for i in range(1, len(items) + 1):
        mark = "☑" if checked_by_n.get(i, True) else "☐"
        cur.append(InlineKeyboardButton(
            f"{mark} {i}", callback_data=f"bt:{i}",
        ))
        if len(cur) == 5:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([
        InlineKeyboardButton("✅ Submit", callback_data="bsub"),
        InlineKeyboardButton("❌ Cancel", callback_data="bcan"),
    ])
    return InlineKeyboardMarkup(rows)


def render_batch_body(
    items: list[dict], total_remaining: int,
    payload_items: list[dict] | None = None,
    *,
    verbose: bool = False,
    header_emoji: str = "📦",
    header_label: str = "Batch",
) -> str:
    """Full numbered DM body with inline ☑/☐ state next to each row.

    Telegram renders the body with a fixed-width font when wrapped in a
    code block. We use that so the columns align even on a phone screen.

    ``verbose=True`` is the Amazon mode — each row spans multiple lines
    so long order summaries (e.g. "Amazon order 112-...: 3 item(s):
    Tidy Drawer; Cat Pump; AWoHH Pack") are visible in full instead of
    being truncated to fit a single column.
    """
    if not items:
        return f"{header_emoji} Your {header_label.lower()} queue is empty. 🎉"

    checked_by_n: dict[int, bool] = {}
    if payload_items:
        checked_by_n = {it["n"]: it.get("checked", True) for it in payload_items}

    n = len(items)
    extra = ""
    if total_remaining > n:
        extra = f" ({total_remaining - n} more after this page)"
    header = f"{header_emoji} {header_label} — {n} items{extra}"

    body_lines = []
    if verbose:
        # Multi-line per row: header line, then each subsequent line is a
        # full-width continuation of the detail (item names, etc.).
        for i, it in enumerate(items, start=1):
            mark = "☑" if checked_by_n.get(i, True) else "☐"
            amt = (it["amount_cents"] or 0) / 100
            date_str = str(it["txn_date"])[5:]
            guess = (it.get("suggested_category_name") or "(no guess)")[:30]
            raw = (it.get("raw_summary") or it.get("payee") or "").strip()
            # Strip any Citi/Chase prefix the same way the inline display does
            import re
            raw = re.sub(
                r"^(?:Citi|Chase|Coastal)[^$]*\$[\d,]+\.\d{2}\s+at\s+",
                "", raw, flags=re.IGNORECASE,
            )
            body_lines.append(
                f"{mark} {i:>2}. {date_str}  ${amt:>+8.2f}  → {guess}"
            )
            # Wrap the detail block onto continuation lines, indented to
            # align under the data — no truncation.
            for chunk in _wrap_text(raw, 56):
                body_lines.append(f"     {chunk}")
            body_lines.append("")  # spacer
        # Drop the trailing spacer
        if body_lines and body_lines[-1] == "":
            body_lines.pop()
    else:
        for i, it in enumerate(items, start=1):
            mark = "☑" if checked_by_n.get(i, True) else "☐"
            amt = (it["amount_cents"] or 0) / 100
            date_str = str(it["txn_date"])[5:]
            display = _best_display_string(it)[:30]
            guess = (it.get("suggested_category_name") or "(no guess)")[:24]
            body_lines.append(
                f"{mark} {i:>2}. {date_str}  ${amt:>+8.2f}  {display:<30}  → {guess}"
            )

    instructions = (
        "Tap any number to ☐ flag it — flagged rows get DM'd individually "
        "after Submit. Or reply with shorthand: `all`, `3=groceries`, `5 skip`."
    )
    return (
        f"{header}\n\n"
        f"```\n" + "\n".join(body_lines) + "\n```\n\n"
        f"{instructions}"
    )


def _wrap_text(text: str, width: int) -> list[str]:
    """Greedy line-wrap on word boundaries. Falls back to mid-word
    truncation only when a single word exceeds ``width``."""
    out: list[str] = []
    cur = ""
    for word in text.split():
        if not cur:
            cur = word[:width] if len(word) > width else word
            continue
        if len(cur) + 1 + len(word) <= width:
            cur = f"{cur} {word}"
        else:
            out.append(cur)
            cur = word[:width] if len(word) > width else word
    if cur:
        out.append(cur)
    return out


def render_batch_message(items: list[dict], total_remaining: int) -> str:
    """Backwards-compat shim; delegates to ``render_batch_body`` so older
    call sites (any shell scripts that imported render_batch_message)
    continue working.
    """
    return render_batch_body(items, total_remaining)


def save_batch_context(
    db_path: Path | str, chat_id: int, items: list[dict],
    message_id: int, *, verbose: bool = False,
    header_emoji: str = "📦", header_label: str = "Batch",
) -> None:
    """Persist the numbered mapping + display options for this batch.

    ``verbose`` is set True for the Amazon view so re-renders on toggle
    preserve the multi-line per-item layout. ``header_emoji`` and
    ``header_label`` keep the header consistent across re-renders.
    """
    payload = {
        "items": [{"n": i + 1, "pt_id": it["pt_id"], "checked": True}
                  for i, it in enumerate(items)],
        "message_id": message_id,
        "sent_at": _utcnow().isoformat(),
        "consumed": False,
        "verbose": verbose,
        "header_emoji": header_emoji,
        "header_label": header_label,
    }
    with storage.connect(db_path) as con:
        con.execute(
            "UPDATE bot_conversation SET last_batch_json = ? WHERE chat_id = ?",
            (json.dumps(payload), chat_id),
        )


def toggle_batch_item(
    db_path: Path | str, chat_id: int, n: int,
) -> dict | None:
    """Toggle the checked state of one numbered item. Returns the updated
    payload (so the caller can re-render the keyboard) or None if no
    batch context.
    """
    payload = load_batch_context(db_path, chat_id)
    if not payload:
        return None
    for it in payload.get("items", []):
        if it.get("n") == n:
            it["checked"] = not it.get("checked", True)
            break
    else:
        return payload
    with storage.connect(db_path) as con:
        con.execute(
            "UPDATE bot_conversation SET last_batch_json = ? WHERE chat_id = ?",
            (json.dumps(payload), chat_id),
        )
    return payload


def load_batch_context(
    db_path: Path | str, chat_id: int,
) -> dict | None:
    """Return the saved batch payload or None if absent/consumed."""
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT last_batch_json FROM bot_conversation WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    if not row or not row["last_batch_json"]:
        return None
    try:
        payload = json.loads(row["last_batch_json"])
    except (ValueError, TypeError):
        return None
    if payload.get("consumed"):
        return None
    return payload


def mark_batch_consumed(db_path: Path | str, chat_id: int) -> None:
    payload = load_batch_context(db_path, chat_id)
    if not payload:
        return
    payload["consumed"] = True
    with storage.connect(db_path) as con:
        con.execute(
            "UPDATE bot_conversation SET last_batch_json = ? WHERE chat_id = ?",
            (json.dumps(payload), chat_id),
        )


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------

# A "decision" tuple = (action, value) where action is:
#   'confirm'    — use the row's suggested_category
#   'override'   — use the named category
#   'skip'       — drop into the skipped pile
#   'back'       — route to Allison (assigned_to_user_id='allison')
ALL_ACTIONS = {"confirm", "override", "skip", "back"}

_NUMBER_RE = re.compile(r"^\d+$")
_KW_SKIP = {"skip", "s"}
_KW_BACK = {"back", "b"}
_NOISE = {"and", "except", ","}


def _split_specifier(text: str) -> list[str]:
    """Tokenize a specifier text:
      - separators: whitespace, commas, semicolons
      - keeps '=' attached to its left number when written as 'N=val'
      - splits 'N = val' into ['N', '=', 'val']
    """
    # Normalize a leading 'N=val' shape (no spaces) into 'N = val' so we
    # can treat '=' as a standalone delimiter token.
    normalized = re.sub(r"(\d+)\s*=\s*", r"\1 = ", text)
    normalized = re.sub(r"[,;]+", " ", normalized)
    return [t for t in normalized.split() if t]


def parse_reply(reply_text: str, items: list[dict]) -> dict[str, Any]:
    """Translate a user's shorthand reply into per-item decisions.

    Grammar:
        REPLY := "all" SPEC?
              |  "all" "except" SPEC
              |  SPEC

        SPEC  := SPEC_ITEM ( SPEC_ITEM )*

        SPEC_ITEM := NUMBER                       # confirm guess
                  |  NUMBER "=" CATEGORY_TEXT     # override with category
                  |  NUMBER ( "skip" | "s" )      # skip
                  |  NUMBER ( "back" | "b" )      # route to Allison

    Returns:
      {
        "decisions": [{"pt_id": int, "action": str, "value": str | None}],
        "unparsed": [str],
        "warnings": [str],
        "applies_to_all": bool,
      }
    """
    if not reply_text:
        return {"decisions": [], "unparsed": [], "warnings": ["empty reply"],
                "applies_to_all": False}

    n_to_pt = {it["n"]: it["pt_id"] for it in items}
    valid_nums = set(n_to_pt.keys())
    text = reply_text.strip()
    lower_full = text.lower()

    decisions_by_num: dict[int, dict] = {}
    unparsed: list[str] = []
    warnings: list[str] = []
    applies_to_all = False

    # Strip leading 'all' / 'all except' once
    all_match = re.match(r"^all\b", lower_full)
    if all_match:
        applies_to_all = True
        # Default: every number gets 'confirm'
        for n, pt_id in n_to_pt.items():
            decisions_by_num[n] = {
                "pt_id": pt_id, "action": "confirm", "value": None,
            }
        except_match = re.search(r"\bexcept\b", lower_full)
        if except_match:
            specifier_text = text[except_match.end():]
        else:
            specifier_text = text[all_match.end():]
    else:
        specifier_text = text

    tokens = _split_specifier(specifier_text)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        tok_l = tok.lower()
        if tok_l in _NOISE:
            i += 1
            continue
        if not _NUMBER_RE.match(tok):
            # Non-number outside of a category context = noise
            unparsed.append(tok)
            i += 1
            continue

        n = int(tok)
        if n not in valid_nums:
            warnings.append(f"item #{n} not in this batch")
            i += 1
            continue
        pt_id = n_to_pt[n]
        next_tok = tokens[i + 1] if i + 1 < len(tokens) else None
        next_l = (next_tok or "").lower()

        if next_l == "=":
            # Collect category text until the next number-or-end
            cat_tokens: list[str] = []
            j = i + 2
            while j < len(tokens):
                nt = tokens[j]
                if _NUMBER_RE.match(nt):
                    break
                if nt.lower() in _NOISE:
                    j += 1
                    continue
                cat_tokens.append(nt)
                j += 1
            cat_text = " ".join(cat_tokens).strip()
            if cat_text:
                decisions_by_num[n] = {
                    "pt_id": pt_id, "action": "override", "value": cat_text,
                }
            else:
                warnings.append(f"item #{n} '=' had no category")
            i = j
            continue

        if next_l in _KW_SKIP:
            decisions_by_num[n] = {
                "pt_id": pt_id, "action": "skip", "value": None,
            }
            i += 2
            continue
        if next_l in _KW_BACK:
            decisions_by_num[n] = {
                "pt_id": pt_id, "action": "back", "value": None,
            }
            i += 2
            continue

        # Bare number → confirm
        decisions_by_num[n] = {
            "pt_id": pt_id, "action": "confirm", "value": None,
        }
        i += 1

    return {
        "decisions": list(decisions_by_num.values()),
        "unparsed": unparsed,
        "warnings": warnings,
        "applies_to_all": applies_to_all,
    }


# ---------------------------------------------------------------------------
# Applying decisions
# ---------------------------------------------------------------------------

def apply_decisions(
    db_path: Path | str, *,
    decisions: list[dict],
    settings,
    categorizer,
    categories: list[dict],
    chat_id: int,
    user_id: str,
) -> dict:
    """Execute each decision. Returns counters + per-item results."""
    from bot.telegram_bot import _apply_choice
    confirmed = 0
    overridden = 0
    skipped = 0
    routed = 0
    failed: list[dict] = []
    no_guess_errors: list[dict] = []

    for d in decisions:
        pt_id = d["pt_id"]
        action = d["action"]
        try:
            if action == "confirm":
                # Use suggested_category from the row; abort if NULL.
                with storage.connect(db_path) as con:
                    row = con.execute(
                        "SELECT suggested_category, status FROM pending_txn WHERE id = ?",
                        (pt_id,),
                    ).fetchone()
                if not row or row["status"] != "pending":
                    failed.append({"pt_id": pt_id, "reason": "no longer pending"})
                    continue
                if not row["suggested_category"]:
                    no_guess_errors.append({"pt_id": pt_id})
                    continue
                status = _apply_choice(
                    settings, categorizer, categories,
                    chat_id=chat_id, user_id=user_id,
                    choice_kind=f"callback:{row['suggested_category']}",
                    payload=None,
                    override_kind="txn", override_id=pt_id,
                )
                if status.lower().startswith("categorized"):
                    confirmed += 1
                else:
                    failed.append({"pt_id": pt_id, "reason": status[:60]})

            elif action == "override":
                status = _apply_choice(
                    settings, categorizer, categories,
                    chat_id=chat_id, user_id=user_id,
                    choice_kind="text", payload=d["value"],
                    override_kind="txn", override_id=pt_id,
                )
                if status.lower().startswith("categorized"):
                    overridden += 1
                else:
                    failed.append({"pt_id": pt_id, "reason": status[:60]})

            elif action == "skip":
                with storage.connect(db_path) as con:
                    con.execute(
                        "UPDATE pending_txn SET status='skipped' WHERE id=?",
                        (pt_id,),
                    )
                storage.audit(db_path, "skipped",
                              {"kind": "txn", "id": pt_id, "via": "batch"})
                skipped += 1

            elif action == "back":
                with storage.connect(db_path) as con:
                    con.execute(
                        "UPDATE pending_txn SET assigned_to_user_id='allison', "
                        "queue_lane='hot', lane_changed_at=CURRENT_TIMESTAMP "
                        "WHERE id=?",
                        (pt_id,),
                    )
                storage.audit(db_path, "routed",
                              {"kind": "txn", "id": pt_id, "via": "batch",
                               "to_user": "allison"})
                routed += 1

        except Exception as e:  # noqa: BLE001
            failed.append({"pt_id": pt_id, "reason": str(e)[:80]})

    return {
        "confirmed": confirmed,
        "overridden": overridden,
        "skipped": skipped,
        "routed": routed,
        "failed": failed,
        "no_guess": no_guess_errors,
    }


def build_awareness_body(
    db_path: Path | str, *, user_id: str, top_n: int = 4,
) -> str | None:
    """Build the body for the 10am/2pm/7pm awareness ping.

    Returns ``None`` when nothing is actionable — both /batch and /amazon
    queues are empty. Otherwise the body covers both surfaces so Steven
    only has to glance at one notification:

        📥 23 batch + 5 Amazon items ready.
           Top batch guesses: Dining (12), Vacation (4), Groceries (3), no guess (4)
           /batch and /amazon to process.

    When one surface is empty, the body collapses gracefully:

        📥 5 Amazon items ready. /amazon to process.
        📥 23 batch items ready. /batch to process.
    """
    batch_n = count_cold_batch(db_path, user_id=user_id)
    amazon_n = count_amazon_ready(db_path, user_id=user_id)
    if batch_n == 0 and amazon_n == 0:
        return None

    # Compose the headline line based on which surfaces have items.
    parts: list[str] = []
    if batch_n:
        parts.append(f"{batch_n} batch")
    if amazon_n:
        parts.append(f"{amazon_n} Amazon")
    headline = f"📥 {' + '.join(parts)} item{'s' if (batch_n + amazon_n) != 1 else ''} ready."

    # When /batch has items, include a top-guesses breakdown so Steven
    # can decide whether it's worth tapping in or letting it accumulate.
    detail_lines: list[str] = []
    if batch_n:
        with storage.connect(db_path) as con:
            rows = con.execute(
                f"""SELECT pt.suggested_category, c.name AS cat_name,
                          COUNT(*) AS n
                   FROM pending_txn pt
                   LEFT JOIN category c ON c.id = pt.suggested_category
                   WHERE pt.assigned_to_user_id = ?
                     AND pt.status = 'pending'
                     AND pt.queue_lane = 'cold'
                     AND NOT {_AMAZON_PAYEE_SQL}
                   GROUP BY pt.suggested_category, c.name
                   ORDER BY n DESC""",
                (user_id,),
            ).fetchall()
        guesses: list[str] = []
        for r in rows[:top_n]:
            nm = r["cat_name"] or "no guess"
            if len(nm) > 22:
                nm = nm[:22] + "…"
            guesses.append(f"{nm} ({r['n']})")
        leftover = sum(int(r["n"]) for r in rows[top_n:])
        if leftover:
            guesses.append(f"other ({leftover})")
        if guesses:
            detail_lines.append(f"   Top batch guesses: {', '.join(guesses)}")

    # Footer line: which commands to type
    cmds: list[str] = []
    if batch_n:
        cmds.append("/batch")
    if amazon_n:
        cmds.append("/amazon")
    footer = f"   Type {' or '.join(cmds)} to process."

    return "\n".join([headline, *detail_lines, footer])


def format_summary(
    result: dict, remaining_cold: int, *, amazon_ready: int = 0,
) -> str:
    """Render the post-Submit summary DM.

    ``remaining_cold`` is the /batch (non-Amazon) backlog left.
    ``amazon_ready`` is the /amazon backlog — surfaced here so Steven
    never has to mentally remember to run /amazon separately.
    """
    parts = []
    if result["confirmed"]:
        parts.append(f"✅ {result['confirmed']} confirmed")
    if result["overridden"]:
        parts.append(f"✏ {result['overridden']} overridden")
    if result["skipped"]:
        parts.append(f"⏭ {result['skipped']} skipped")
    if result["routed"]:
        parts.append(f"➡ {result['routed']} to Allison")
    main = "  ".join(parts) if parts else "Nothing was changed."

    extras = []
    if result["no_guess"]:
        n = len(result["no_guess"])
        extras.append(f"⚠ {n} item(s) had no guess to confirm — use 'N=category' to set them.")
    if result["failed"]:
        n = len(result["failed"])
        extras.append(f"⚠ {n} item(s) failed: {result['failed'][:2]}")

    footer_lines: list[str] = []
    if remaining_cold > 0:
        footer_lines.append(
            f"{remaining_cold} items remain in /batch."
        )
    else:
        footer_lines.append("🎉 Batch queue is empty.")
    if amazon_ready > 0:
        footer_lines.append(
            f"📦 {amazon_ready} Amazon item{'s' if amazon_ready != 1 else ''} "
            f"ready — /amazon."
        )

    return (
        main
        + ("\n\n" + "\n".join(extras) if extras else "")
        + "\n\n" + "\n".join(footer_lines)
    )
