# Wide Display Mode

**Date:** 2026-08-01
**Status:** Design approved
**Implemented in:** `ynabhelper-ui` (Tauri desktop app)

## Problem

Steven moved to a 5K monitor at 200% scaling, which gives a 2560 × 1440
logical viewport. The app doesn't use it.

`PageShell` caps every page at `max-w-7xl` (1280px) and centres it. That
cap is the only one of its kind in the codebase — every page inherits it,
so every page is affected.

| | |
|---|---:|
| Window width, maximized | 2560px |
| less sidebar (`w-60`) | −240px |
| less page padding (`xl:px-10`) | −80px |
| **Available for content** | **2240px** |
| **Actually used** | **1280px** |
| Wasted | 960px — 43% |

Tailwind's breakpoints also stop at `2xl` (1536px), so the layout has no
concept of a screen wider than that.

A second, compounding problem: `tauri.conf.json` opens the window at
1280×800 and nothing remembers the size. Every launch puts a 1280px window
on a 2560px desktop, so even a perfect width fix would do nothing until the
window is manually maximized — every time.

## Decisions

Locked with Steven before design:

1. **The complaint is wasted space, not small text.** Windows DPI scaling
   already handles sizing; the type scale is rem-based and correct. This
   design does not touch font sizes.
2. **"Just go wider."** Same layouts, wider. No reflowing 2-up cards to
   4-up, no master–detail panes. Those are separate decisions.
3. **A toggle, not automatic breakpoints.** With one machine and two very
   different screens, a switch set once per monitor is more predictable
   than a layout that reflows while dragging a window.
4. **Root class + CSS**, over a React context. Tailwind is already
   configured `darkMode: ["class"]`, so "a root class changes presentation"
   is the pattern a reader of this codebase will expect.
5. **Window-state persistence is in scope.** Without it the toggle appears
   broken on launch, and both are the same outcome from the user's side.

## Non-goals

- Font or spacing scale changes.
- Automatic responsive breakpoints above `2xl`.
- Master–detail panes or any layout restructuring.
- Wiring up dark mode. `styles.css` defines a complete `.dark` theme that
  nothing ever activates — this design is the first code to manipulate a
  root class, but adopting dark mode is a separate decision.
- Any change to the mobile web UI, which shares this SPA.

---

## 1. Width toggle

### `src/lib/useWideMode.ts`

Owns one boolean.

- Reads `hb.wide` from `localStorage`, matching the existing `hb.token`
  convention in `TokenGate.tsx`. Stored as the string `"1"` for on; absent
  or anything else means off, so an unset key defaults to today's
  behaviour.
- Applies or removes a `wide` class on `document.documentElement`.
- Returns `[wide, setWide]`.

**The initial read happens at module load, not inside an effect.** React
effects run after first paint, so an effect-based read would show one frame
at 1280px on every launch before snapping wide.

### The width constraint moves from PageShell into CSS

`PageShell` currently hardcodes `max-w-7xl mx-auto`. Replace with a
`page-container` class, defined in `styles.css`:

```css
@layer components {
  .page-container { max-width: 80rem; margin-inline: auto; }
  html.wide .page-container { max-width: 2400px; }
}
```

Defining both states as ordinary rules in the same layer avoids a
specificity fight with Tailwind's utilities — which is why the base
`max-width` moves into CSS rather than staying a utility class that a
`html.wide` rule would have to out-rank.

`PageShell` is the single place `max-w-7xl` appears, so every page picks
this up with no per-page edits.

### Why 2400px and not uncapped

At Steven's 200% scaling the content area tops out around 2240px, so **the
cap never engages** — he gets the full width he asked for. It exists for
the other scaling factors: at 150% content would reach ~3100px and at 100%
~4900px, which puts a transaction's payee and its amount at opposite ends
of a 32-inch screen.

The cap costs nothing in the real configuration and prevents the
pathological one.

### Toggle placement

The sidebar footer, beside the version number — currently the only thing in
that strip. A lucide icon plus a label.

**The label names the current state, not the action**: it reads "Wide" when
wide mode is on and "Standard" when it is off. A button labelled with what
it will do is ambiguous next to an icon that shows what is; naming the
state matches how the rest of this sidebar reads.

This placement scopes the feature for free: the sidebar is
`hidden md:flex`, so the control never renders on the mobile web UI. Should
`hb.wide` somehow be set on a phone, the CSS is harmless — a phone viewport
is narrower than either cap.

## 2. Window sizing

### `tauri-plugin-window-state`

`src-tauri` is Tauri 2.0 with no plugins registered. Add the official
window-state plugin — one dependency in `Cargo.toml`, one `.plugin(...)`
line in the builder. It persists size and position across launches.

Preferred over hand-rolled bounds storage: it handles multi-monitor
coordinates, off-screen recovery, and maximized state, all of which are
easy to get subtly wrong.

### Default window size

Raise `tauri.conf.json` from 1280×800 to **1800×1100**. This only applies
on a fresh install before any state is saved, but it means the very first
launch on a large monitor looks right.

Leave `minWidth: 1024` / `minHeight: 640` unchanged — the layout is not
designed below that, and the plugin restoring a tiny window should still be
floored.

## 3. Verification

This repo has **no test framework**. Verification is:

- `npx tsc --noEmit` — clean
- `npm run build` — succeeds
- Visual confirmation on the actual monitor

The Rust dependency means a full rebuild and an exe hot-swap over
`C:\Users\Steven\AppData\Local\Harris Budget\` before any of it is visible.

Specifically worth confirming by eye, because nothing else can catch them:

- No narrow flash on launch (the module-load read).
- The toggle persists across a full quit and relaunch.
- The window reopens at its previous size and position.
- A page with a wide table (Transactions) genuinely fills the width, and a
  page with cards (Budget) doesn't look stranded.

## Risks

**A Rust dependency for a layout feature.** `tauri-plugin-window-state` is
first-party and small, but it does mean this change can't ship as a
JS-only bundle update — it needs a full rebuild and exe swap. Accepted
because the window-size problem can't be solved from JS.

**The `wide` class is global.** Anything that later wants to opt out of
wide mode must say so explicitly. With one consumer today that's fine; if a
second appears, the rule should move behind a more specific selector rather
than accumulating exceptions.
