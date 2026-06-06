"""Two-tier lookup for "what kind of business is this payee?"

Used to give the categorizer context for payees the user has never
transacted with before (where historical priors are empty). Each payee
is looked up at most once — the result is cached in the `payee_intel`
table forever (until a manual refresh).

Tier 1: qwen3:32b's own training data
  Just ask the local LLM "What kind of business is X?" — quick, free,
  works for the vast majority of named merchants. Returns a one-sentence
  description plus a self-reported confidence score. If confidence < 0.4
  we fall through to tier 2.

Tier 2: DuckDuckGo Instant Answer API
  Free, no auth, returns Abstract / AbstractText / Heading. Tiny payload.
  Often empty for niche queries but useful for established brands. When
  it returns useful text, qwen synthesizes a short description from it.

All errors degrade to source='none' with description=NULL so the caller
can decide to skip the intel injection rather than crash.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from bot import storage

log = logging.getLogger(__name__)


_LLM_SYSTEM_PROMPT = """You are a business-name research assistant.

You will be given the name of a merchant or payee. Your job is to identify what
kind of business it is in one short sentence (under 25 words). Be specific —
"sells athletic socks via Amazon" beats "online retailer". Include the broad
category (food, household goods, clothing, software service, etc.).

You must ALSO self-report a confidence score:
- 0.9+ : household-name brand (Amazon, Netflix, Costco)
- 0.7-0.9 : recognizable smaller brand or known niche merchant
- 0.4-0.7 : guessing based on name fragments / partial recognition
- < 0.4  : you don't actually know what this is

If you don't know, RETURN confidence < 0.4 — do NOT make something up.

Respond ONLY with a JSON object:
{
  "description": "<one sentence, or empty string if unknown>",
  "confidence": <0.0 to 1.0>
}
No text outside the JSON.
"""


def _normalize(payee: str) -> str:
    return (payee or "").strip().lower()


def _ask_llm(
    endpoint: str, model: str, payee: str, temperature: float = 0.2
) -> dict[str, Any]:
    """Returns {description, confidence}. Empty description on failure."""
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{endpoint.rstrip('/')}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                        {"role": "user",
                         "content": f"Payee name: {payee}"},
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": temperature},
                },
            )
            data = resp.json()
        parsed = json.loads(data["message"]["content"])
        return {
            "description": (parsed.get("description") or "").strip(),
            "confidence": float(parsed.get("confidence") or 0.0),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("payee_intel LLM call failed for %r: %s", payee, e)
        return {"description": "", "confidence": 0.0}


def _ddg_lookup(payee: str) -> str:
    """Returns a short summary from DuckDuckGo Instant Answer API, or ""."""
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(
                "https://api.duckduckgo.com/",
                params={
                    "q": payee,
                    "format": "json",
                    "no_html": "1",
                    "skip_disambig": "1",
                },
            )
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning("payee_intel DDG failed for %r: %s", payee, e)
        return ""

    # AbstractText is the main result. If empty, the first RelatedTopic text
    # is the next best thing.
    text = (data.get("AbstractText") or "").strip()
    if not text:
        related = data.get("RelatedTopics") or []
        if related and isinstance(related, list):
            first = related[0]
            if isinstance(first, dict):
                text = (first.get("Text") or "").strip()
    return text[:500]  # cap to keep prompt size sane


def _summarize_ddg(
    endpoint: str, model: str, payee: str, ddg_text: str,
    temperature: float = 0.2,
) -> dict[str, Any]:
    """Have qwen turn DDG raw text into our one-sentence shape."""
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{endpoint.rstrip('/')}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _LLM_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": (
                                f"Payee name: {payee}\n\n"
                                f"Web search result snippet:\n{ddg_text}\n\n"
                                f"Use the snippet to describe this business. "
                                f"If the snippet looks unrelated to the payee, "
                                f"confidence stays low."
                            ),
                        },
                    ],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": temperature},
                },
            )
            data = resp.json()
        parsed = json.loads(data["message"]["content"])
        return {
            "description": (parsed.get("description") or "").strip(),
            "confidence": float(parsed.get("confidence") or 0.0),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("payee_intel DDG-summarize failed for %r: %s", payee, e)
        return {"description": "", "confidence": 0.0}


def get_intel(
    db_path: Path | str, payee: str, *, endpoint: str, model: str,
    use_web: bool = True, llm_threshold: float = 0.4,
) -> dict[str, Any] | None:
    """Returns the cached or freshly-fetched intel row for `payee`.

    Cache hit is keyed on lowercased payee. Cache miss runs:
      1. LLM-knowledge tier. If confidence >= `llm_threshold`, store + return.
      2. If `use_web` and tier 1 was weak, DuckDuckGo lookup + LLM summarize.
      3. Store whatever we got (even source='none') so we don't re-query.

    Returns None for an empty/whitespace payee.
    """
    norm = _normalize(payee)
    if not norm:
        return None

    # Cache check
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT * FROM payee_intel WHERE payee_norm = ?", (norm,),
        ).fetchone()
    if row:
        return dict(row)

    # Tier 1: ask qwen
    llm = _ask_llm(endpoint, model, payee)
    description = llm["description"]
    confidence = llm["confidence"]
    source = "llm_knowledge"

    # Tier 2: web fallback when LLM doesn't know
    if confidence < llm_threshold and use_web:
        ddg_text = _ddg_lookup(payee)
        if ddg_text:
            web = _summarize_ddg(endpoint, model, payee, ddg_text)
            if web["confidence"] > confidence:
                description = web["description"]
                confidence = web["confidence"]
                source = "duckduckgo"

    # Persist (even if we got nothing — locks the cache so we don't re-fetch)
    final_source = source if description else "none"
    with storage.connect(db_path) as con:
        con.execute(
            """INSERT INTO payee_intel
                 (payee_norm, payee_raw, description, source, confidence)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(payee_norm) DO UPDATE SET
                 description = excluded.description,
                 source = excluded.source,
                 confidence = excluded.confidence,
                 fetched_at = CURRENT_TIMESTAMP
            """,
            (norm, payee, description or None, final_source, confidence),
        )
    log.info("payee_intel %s: source=%s conf=%.2f desc=%r",
             payee, final_source, confidence, description[:60])

    return {
        "payee_norm": norm,
        "payee_raw": payee,
        "description": description or None,
        "source": final_source,
        "confidence": confidence,
    }
