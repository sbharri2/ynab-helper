"""Ollama-backed category suggester.

Sends a structured prompt to a local Ollama instance and parses the
JSON-mode reply into ``{category_id, confidence, reasoning}``. All errors
(connection refused, timeout, malformed JSON, missing keys) are caught
and converted into a None-category fallback so the bot keeps working
even when Ollama is offline.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a personal-finance assistant that assigns transactions to YNAB categories.

You will be given:
- A transaction summary (payee, items, memo)
- The amount in cents
- The transaction date
- The source (e.g. amazon, venmo)
- A list of available categories, each with id, name, and group
- (Sometimes) historical priors showing how this payee has been categorized before

Pick the SINGLE best category from the provided list.

RULES (never violate):
1. The provided category list ALREADY excludes credit-card-payment buckets and
   scheduled-bill categories. Pick from what's there; do not invent or imagine
   others.
2. If historical priors are provided, the answer is almost always one of the
   top-listed categories. Override only when the transaction summary clearly
   shows a different intent (e.g. "Amazon" priors say Groceries but the items
   are a treadmill — pick Exercise).
3. If you are less than 0.5 confident, return category_id=null. A null answer
   is BETTER than a wrong one — the user will categorize blank.

Respond ONLY with a JSON object matching this exact shape:
{
  "category_id": "<one of the provided ids, or null if none fit>",
  "confidence": <float between 0.0 and 1.0>,
  "reasoning": "<one short sentence>"
}

Do not include any text outside the JSON object. Do not invent category ids
that were not in the provided list.
"""


class Categorizer:
    def __init__(
        self,
        endpoint: str,
        model: str,
        temperature: float = 0.3,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.temperature = temperature

    def _build_user_message(
        self,
        *,
        summary: str,
        amount_cents: int,
        date_str: str,
        source: str,
        categories: list[dict],
        priors: list[dict] | None = None,
    ) -> str:
        amount_dollars = amount_cents / 100.0
        lines = [
            f"Source: {source}",
            f"Date: {date_str}",
            f"Amount: ${amount_dollars:.2f}",
            f"Summary: {summary}",
        ]
        if priors:
            lines.append("")
            lines.append("Historical priors for this payee (Steven's past 2 years):")
            for p in priors:
                pct = p.get("pct", 0) * 100
                cname = p.get("category_name") or "(uncategorized)"
                n = p.get("count", 0)
                total = (p.get("total_cents") or 0) / 100
                lines.append(
                    f"- {pct:.1f}% {cname} ({n} transactions, ${total:,.0f} total)"
                )
            lines.append("Strongly prefer one of these unless the summary clearly points elsewhere.")
        lines.append("")
        lines.append("Available categories:")
        for c in categories:
            # Accept either {"group": ...} (legacy YnabClient shape) or
            # {"group_name": ...} (new storage.list_categories_for_spending shape).
            group = c.get("group") or c.get("group_name") or ""
            lines.append(f"- {c['id']} | {group} > {c['name']}")
        return "\n".join(lines)

    def suggest_topn(
        self,
        *,
        summary: str,
        amount_cents: int,
        date_str: str,
        source: str,
        categories: list[dict],
        priors: list[dict] | None = None,
        intel: str | None = None,
        n: int = 4,
    ) -> list[dict[str, Any]]:
        """Rank top-N categories instead of picking one.

        Used by the Telegram keyboard builder when historical priors
        don't supply enough educated guesses. Returns up to ``n`` dicts
        of {category_id, confidence} sorted by confidence descending.

        ``intel`` is an optional one-sentence description of the payee
        (from ``bot.payee_intel.get_intel``) injected into the prompt so
        qwen can rank based on what kind of business it actually is —
        not just what the merchant name looks like.

        Returns ``[]`` on any failure so the caller can fall back gracefully.
        """
        topn_system = SYSTEM_PROMPT + (
            "\n\nWhen asked for a ranked list, return JSON of the shape "
            "{\"candidates\": [{\"category_id\": ..., \"confidence\": ...}, ...]}. "
            "Sort by confidence descending. Each category_id MUST be from the "
            "provided list. Do not return a category_id more than once."
        )
        try:
            user_msg = self._build_user_message(
                summary=summary,
                amount_cents=amount_cents,
                date_str=date_str,
                source=source,
                categories=categories,
                priors=priors,
            )
            if intel:
                user_msg += f"\n\nWhat we know about the payee: {intel}"
            user_msg += (
                f"\n\nReturn your top {n} category guesses ranked by confidence."
            )
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": topn_system},
                    {"role": "user", "content": user_msg},
                ],
                "stream": False,
                "format": "json",
                "options": {"temperature": self.temperature},
            }
            with httpx.Client(timeout=90.0) as client:
                resp = client.post(f"{self.endpoint}/api/chat", json=payload)
                data = resp.json()
            content = data["message"]["content"]
            parsed = json.loads(content)
            cands = parsed.get("candidates") or []
            valid_ids = {c["id"] for c in categories}
            out: list[dict[str, Any]] = []
            seen: set[str] = set()
            for c in cands:
                cid = c.get("category_id")
                if cid in valid_ids and cid not in seen:
                    out.append({
                        "category_id": cid,
                        "confidence": float(c.get("confidence") or 0.0),
                    })
                    seen.add(cid)
                if len(out) >= n:
                    break
            return out
        except Exception as e:  # noqa: BLE001
            log.warning("Categorizer.suggest_topn failed: %s", e)
            return []

    def suggest(
        self,
        *,
        summary: str,
        amount_cents: int,
        date_str: str,
        source: str,
        categories: list[dict],
        priors: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Ask Ollama to pick a category. Always returns a dict; never raises.

        When ``priors`` is supplied (typically from
        ``storage.get_category_priors_for_payee``), the user message includes
        a historical-distribution block that strongly biases the model toward
        categories Steven has used for this payee before.
        """
        try:
            user_msg = self._build_user_message(
                summary=summary,
                amount_cents=amount_cents,
                date_str=date_str,
                source=source,
                categories=categories,
                priors=priors,
            )
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "stream": False,
                "format": "json",
                "options": {"temperature": self.temperature},
            }
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(f"{self.endpoint}/api/chat", json=payload)
                data = resp.json()
            content = data["message"]["content"]
            parsed = json.loads(content)
            return {
                "category_id": parsed.get("category_id"),
                "confidence": float(parsed.get("confidence", 0.0)),
                "reasoning": parsed.get("reasoning", ""),
            }
        except Exception as e:  # noqa: BLE001 - graceful fallback by design
            log.warning("Categorizer.suggest failed: %s", e)
            return {
                "category_id": None,
                "confidence": 0.0,
                "reasoning": "",
                "error": str(e),
            }
