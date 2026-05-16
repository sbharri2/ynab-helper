# Appendix: Parsing knowledge inherited from existing Chrome extension

**Companion to:** `2026-05-16-ynab-helper-design.md`
**Source:** Reverse-engineered from `chrome-extension/content/amazon-scraper.js`, `payments-scraper.js`, `venmo-scraper.js` — patterns that have been validated against real Amazon/Venmo pages.

This document captures *transferable* patterns from the existing web scrapers that will likely also work in HTML email parsing. Selectors are page-specific (don't carry over), but textual patterns, regex, and defensive structure all do.

---

## Amazon — transferable patterns

### Order ID format (golden — verbatim across surfaces)

```
\d{3}-\d{7}-\d{7}
```

Format: `123-4567890-1234567`. Same in URLs, web pages, order confirmation emails, shipment emails, and YNAB memo when carrier provides it. **This is the highest-confidence join key between an email and a YNAB charge** — if YNAB's import includes it in the memo, the matcher's `memo_bonus` term fires.

### Date phrases (verbatim in emails)

The web scraper hunts for these substrings — same language appears in confirmation emails:

- `Order placed: <Month D, YYYY>` (most reliable — first-class field in confirmation emails)
- `Order placed: <Month D>` (without year — Amazon sometimes omits, infer current year unless month is in the future relative to today, then year-1)
- `Ordered on <Month D, YYYY>`
- `Arriving <Month D>` (delivery estimate, NOT order date — filter out)
- `Delivered <Month D>` (post-shipment email, NOT order date)

### Total phrase (verbatim in emails)

```
/Total[:\s]*\$?([\d,]+\.?\d{2})/i
```

The web scraper's regex uses `Total[:\s]*` — matches `Total: $159.99`, `Total $159.99`, `Order Total: $159.99`. Confirmation emails use the same wording. **Caveat:** emails also contain "Item Subtotal", "Shipping Total", "Tax Total" — first match may be wrong. Strategy: prefer matches near "Order Total" specifically, or take the largest dollar amount in the email (the scraper's fallback heuristic).

### Item titles

- Length range: **5–200 chars** (the scraper's validation bounds — anything outside is junk)
- In confirmation emails: usually inside `<a>` tags linking to `/dp/<ASIN>` or `/gp/product/<ASIN>` (same href pattern as web)
- Deduplication needed (same item can appear multiple times in HTML)
- Image alt text is a fallback source if `<a>` parsing fails

### Sender variants to expect (need to confirm with inspection)

Best guess based on common Amazon patterns:

| Sender | Purpose | Useful to parse? |
|---|---|---|
| `auto-confirm@amazon.com` | Order confirmation — has items + total | **Yes — primary target** |
| `shipment-tracking@amazon.com` | Items shipped — has order_id, tracking | No (no categorization value) |
| `order-update@amazon.com` | Delays, changes, refund notices | Maybe (refunds eventually) |
| `digital-no-reply@amazon.com` | Digital purchases (Kindle, MP3) | Yes if Steven buys these |
| `subscribe@amazon.com` | Subscribe & Save recurring | Maybe (could be useful for recurring) |
| `marketplace-messages@amazon.com` | Third-party seller messages | No |

**Gmail query for MVP-1 (assumption to validate):**
```
from:auto-confirm@amazon.com newer_than:1d
```

If that misses important cases (Whole Foods orders, digital purchases), expand to `from:amazon.com` + subject pattern matching.

---

## Venmo — transferable patterns

### Direction phrases (verbatim in emails)

The web scraper detects these in headlines — Venmo emails use the same language verbatim:

| Pattern | Meaning | YNAB impact |
|---|---|---|
| `You paid X` | Outgoing → expense | Charge appears in YNAB |
| `You charged X` | Outgoing request → eventual expense when X pays | No YNAB impact until paid |
| `X paid you` | Incoming → income | Inflow in YNAB |
| `X charged you` | Incoming request → eventual expense when you pay | No YNAB impact until paid |

**For MVP, we only care about `You paid` (outgoing money out) and `X paid you` (incoming money in)** — categorization is needed for both.

### Memo extraction

The web scraper extracts memo from `[class*="storyContent"]` — emails put the memo in the body, usually as a paragraph below the headline. Plain text extraction should work.

### Amount format

```
[+-]?\s*\$?([\d,]+\.\d*)
```

The leading `+` or `-` (when present in some Venmo surfaces) indicates direction. Emails typically just show `$42.00` without sign — direction comes from the headline phrase.

### Sender (assumption to validate)

```
from:venmo@venmo.com
```

The user's existing `gmail_cleanup.py` categorizes `venmo.com` as Receipts and `email.venmo.com` as Newsletters — confirming the transactional/marketing split. The MVP query should match the transactional subdomain only.

### Caveat: notification toggle

User must enable per-transaction email notifications in Venmo app:
**Me → Settings (⚙️) → Notifications → Email → Payments sent + Payments received**

Without this, no emails arrive and the Venmo flow silently does nothing. The README should call this out.

---

## Defensive parser structure (reusable approach)

The existing scrapers use a multi-stage fallback pattern that's worth replicating:

```python
def parse_amazon_email(html: str) -> dict:
    # Stage 1: Try the structured signal (a specific HTML element/regex)
    # Stage 2: If that fails, fall back to a broader textual pattern
    # Stage 3: If THAT fails, fall back to a heuristic (e.g., largest dollar amount)
    # Stage 4: If everything fails, log raw HTML + return partial dict with `parse_status: 'partial'`
    ...
```

Web scrapers fail constantly because pages change — emails are more stable (Amazon's template changes are infrequent and version-tagged) but the same defensive philosophy applies. **Never raise on parse failure; always return a partial dict and let the bot tell the user.**

---

## What still needs live validation

The above is reasoning from web-scraper code. Confirming against actual emails requires the OAuth re-auth and `scripts/inspect_receipts.py` run, which is blocked on the user's desktop access. Things to specifically verify when unblocked:

1. **Exact sender addresses Amazon uses** — confirm `auto-confirm@amazon.com` vs alternatives
2. **Whether the order ID appears in the email body in parseable form** (it almost certainly does, but let's see the HTML)
3. **Subject line patterns** — useful as a secondary filter (e.g., "Your Amazon.com order of...")
4. **How items are listed** — bulleted? tabled? linked? quantity field?
5. **For Venmo: confirm `venmo@venmo.com` is the actual sender vs `noreply@venmo.com` or other**
6. **Any "you have a refund" emails** — would help with the refund handling case
7. **Whole Foods Market via Amazon** — does it use a different sender or template?
