# Design fix execution tracker

Design principle: surface exceptions with enough evidence to act, confirm every
action, and otherwise stay out of the way.

## Shipped locally — 2026-07-11

- [x] Clean React/Vite production build and Rust compile.
- [x] Visible desktop version in the persistent app chrome.
- [x] Scroll-safe grouped navigation with one visible vertical scrollbar.
- [x] Today landing page for attention, recent decisions, and system state.
- [x] Consolidated Inbox into the Transactions `Needs attention` view.
- [x] Moved reconciliation, assistant activity, backlog, sync, and bot controls
  under System.
- [x] Added consolidated loopback health data: API version, latest email capture,
  latest YNAB sync, latest Telegram input/output, attention count, and open
  household questions.
- [x] Added visible `Posting…`, household-post confirmation time, inline failure,
  and retry guidance to Ask household actions.
- [x] Added shared focus-visible and reduced-motion behavior.
- [x] Added dialog semantics, accessible close labels, initial focus, focus
  restoration, and viewport-safe modal scrolling.
- [x] Improved tertiary text contrast and responsive page headers.
- [x] Simplified Budget's instructional subtitle.
- [x] Renamed and visually aligned the historical browser importer.
- [x] Built, hash-verified, installed, and relaunched the desktop executable.
- [x] Restarted the scheduled bot owner and verified the new health response live.

## Next release — transaction evidence and decision states

- [ ] Replace generic uncategorized rows with explicit states: Needs decision,
  Waiting on household, Automatically filed, Missing evidence, and Failed.
- [ ] Present receipt, human history, person/card, trip, and merchant-pattern
  evidence in that priority order without algorithm scores.
- [ ] Add a compact “Why?” evidence disclosure on transaction rows.
- [ ] Keep completed decisions visible briefly and offer safe Undo.
- [ ] Add explicit partial-success receipts to bulk categorization.
- [ ] Add a Recently automated saved view to Transactions.

## System and reconciliation release

- [ ] Replace the technical Bot Control default with a plain-language service
  freshness list and contextual recovery actions.
- [ ] Group reconciliation into Missing from YNAB, Missing locally, Possible
  duplicate, and Unmatched receipt issue queues.
- [ ] Move raw identifiers, scores, and engine events behind Diagnostics.
- [ ] Rename the Chat page itself to Assistant activity / Decision history.
- [ ] Link transaction → Telegram question → final decision in both directions.

## Accessibility and responsive completion

- [ ] Complete focus trapping inside shared dialogs.
- [ ] Replace clickable table rows with keyboard-reachable controls.
- [ ] Label all remaining search inputs, checkboxes, and icon-only buttons.
- [ ] Remove meaningful 10px text across Reconciler and analytics.
- [ ] Audit AA contrast for positive, negative, warning, chart, and treemap colors.
- [ ] Verify every core workflow at 1024×640 and by keyboard alone.

## Telegram and notification completion

- [ ] Ensure ignored questions never gate any subsequent processing.
- [ ] Standardize receipts for filed, rerouted, already handled, and ambiguous
  replies.
- [ ] Add user-facing notification levels: Important only, Questions and
  summaries, All activity.
- [ ] Summarize routine auto-filing rather than narrating every transaction.

## Measurement

- [ ] Record action completion/failure counts.
- [ ] Record silent outage duration by service.
- [ ] Track time from Needs attention to resolution.
- [ ] Track prompts with actionable evidence.
- [ ] Track suggestion acceptance/correction and duplicate chat questions.
- [ ] Track manual terminal interventions and installed-build mismatches.
