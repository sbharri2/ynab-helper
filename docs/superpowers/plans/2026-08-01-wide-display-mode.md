# Wide Display Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the desktop app fill a large monitor, via a toggle, and remember its window size between launches.

**Architecture:** A `wide` class on `<html>`, driven by a small hook that persists to `localStorage`, switches a single CSS rule controlling the page container's max width. `PageShell` is the only place that cap exists, so every page inherits it. Separately, the official `tauri-plugin-window-state` persists window geometry, because a width fix is invisible while the window keeps opening at 1280px.

**Tech Stack:** React 18 + TypeScript, Tailwind CSS, Tauri 2.0 (Rust), lucide-react icons.

**Spec:** `C:/Users/Steven/ynabhelper/docs/superpowers/specs/2026-08-01-4k-display-mode-design.md`

## Global Constraints

- **Work in `C:/Users/Steven/ynabhelper-ui`** — a DIFFERENT repository from the bot at `C:/Users/Steven/ynabhelper`. The spec and this plan live in the bot repo; all code changes land in the UI repo.
- **This repo has NO test framework.** No vitest, no jest, no runner of any kind. Verification is `npx tsc --noEmit`, `npm run build`, and looking at the screen. **Do not invent test files** — a plan step that says "write a failing test" would be fiction here. Where a step needs proof, it names the exact command or the exact thing to look at.
- **A parallel session commits to this repo.** Never `git add -A`; stage explicit paths only. If files you aren't touching show as modified, leave them.
- **Never touch the live database** at `C:/Users/Steven/ynabhelper/ynab_helper.db` and never POST to the running API at `127.0.0.1:8765`.
- **House idioms:** `cn()` from `@/lib/cn`, lucide-react for icons, `hb.` prefix for localStorage keys (existing: `hb.token`).
- **`hb.wide` stores the string `"1"` for on.** Absent or any other value means off, so an unset key reproduces today's behaviour exactly.
- **The toggle's label names the current state, not the action** — it reads "Wide" when wide mode is on.
- The width cap in wide mode is **2400px**. At the target 200%-scaling setup it never engages; it exists to stop a 100%-scaling session stretching content past 4900px.
- Leave `minWidth: 1024` / `minHeight: 640` in `tauri.conf.json` unchanged.

## File Structure

**Create:**
- `src/lib/useWideMode.ts` — owns the boolean, its persistence, and the root class
- `src/components/WideModeToggle.tsx` — the sidebar-footer button

**Modify:**
- `src/styles.css` — `.page-container` rules inside the existing `@layer components` block (starts line 78)
- `src/components/PageShell.tsx:13` — swap the hardcoded cap for the class
- `src/App.tsx:163-165` — sidebar footer gains the toggle
- `src-tauri/Cargo.toml` — add the window-state plugin
- `src-tauri/src/lib.rs:24` — register the plugin
- `src-tauri/tauri.conf.json` — raise the default window size

---

## Task 1: Width toggle, end to end

Delivers a working toggle: click it, the page widens, and the choice survives a relaunch. Kept as one task because a hook with no consumer isn't independently verifiable — you can only confirm this by seeing the layout change.

**Files:**
- Create: `src/lib/useWideMode.ts`
- Create: `src/components/WideModeToggle.tsx`
- Modify: `src/styles.css` (inside `@layer components`, which starts at line 78)
- Modify: `src/components/PageShell.tsx` (line 13)
- Modify: `src/App.tsx` (sidebar footer, ~line 163)

**Interfaces:**
- Consumes: `cn` from `@/lib/cn`; `Maximize2`, `Minimize2` from `lucide-react`
- Produces: `useWideMode(): [boolean, (on: boolean) => void]` from `@/lib/useWideMode`; default export `WideModeToggle` from `@/components/WideModeToggle`; CSS class `.page-container`

- [ ] **Step 1: Read the three files you're modifying**

Read `src/components/PageShell.tsx` in full, `src/styles.css` lines 78-128, and `src/App.tsx` lines 125-170. You need to see the existing sidebar footer markup before editing it, and the `@layer components` block's conventions before adding to it.

- [ ] **Step 2: Create the hook**

Create `src/lib/useWideMode.ts`:

```ts
import { useCallback, useState } from "react";

const KEY = "hb.wide";
const CLASS = "wide";

/** Stored as "1" for on. An absent key means off, so an unset preference
 *  reproduces the historical layout exactly. */
function readStored(): boolean {
  try {
    return localStorage.getItem(KEY) === "1";
  } catch {
    // Some webview configurations throw on localStorage access. A missing
    // preference is not worth crashing the app over.
    return false;
  }
}

function apply(on: boolean): void {
  document.documentElement.classList.toggle(CLASS, on);
}

// Applied at MODULE LOAD, not in an effect. Effects run after first paint,
// so an effect-based read would show one frame at the narrow width on every
// launch before snapping wide.
apply(readStored());

/** Wide display mode: lifts the page container's width cap for large
 *  monitors. Returns the current state and a setter. */
export function useWideMode(): [boolean, (on: boolean) => void] {
  const [wide, setWideState] = useState(readStored);

  const setWide = useCallback((on: boolean) => {
    try {
      if (on) localStorage.setItem(KEY, "1");
      else localStorage.removeItem(KEY);
    } catch {
      // Preference won't persist; the session still honours the change.
    }
    apply(on);
    setWideState(on);
  }, []);

  return [wide, setWide];
}
```

- [ ] **Step 3: Add the CSS rules**

In `src/styles.css`, inside the existing `@layer components { ... }` block (opens at line 78), add:

```css
  /* Page width. Standard is the historical max-w-7xl (80rem). Wide mode is
     for large monitors: at 200% scaling on a 5K display the content area is
     about 2240px, so the 2400px ceiling never engages there — it exists to
     stop a 100%-scaling session stretching a table past 4900px.

     Both states live here rather than as a Tailwind utility plus an
     override, so there's no specificity fight to lose. */
  .page-container {
    max-width: 80rem;
    margin-inline: auto;
  }
  html.wide .page-container {
    max-width: 2400px;
  }
```

- [ ] **Step 4: Point PageShell at the class**

In `src/components/PageShell.tsx`, line 13 currently reads:

```tsx
    <div className="px-5 py-6 xl:px-10 xl:py-8 max-w-7xl mx-auto">
```

Replace with:

```tsx
    <div className="px-5 py-6 xl:px-10 xl:py-8 page-container">
```

`max-w-7xl` and `mx-auto` both move into the CSS rule — do not leave them on the element, or the utility will fight the `html.wide` rule.

- [ ] **Step 5: Create the toggle component**

Create `src/components/WideModeToggle.tsx`:

```tsx
import { Maximize2, Minimize2 } from "lucide-react";
import { useWideMode } from "@/lib/useWideMode";
import { cn } from "@/lib/cn";

/** Sidebar-footer switch for wide display mode.
 *
 *  The label names the CURRENT state, not the action — "Wide" means wide
 *  mode is on. A button labelled with what it will do, sitting beside an
 *  icon showing what is, reads ambiguously. */
export default function WideModeToggle() {
  const [wide, setWide] = useWideMode();
  const Icon = wide ? Maximize2 : Minimize2;

  return (
    <button
      type="button"
      onClick={() => setWide(!wide)}
      aria-pressed={wide}
      title={
        wide
          ? "Wide layout — click for standard width"
          : "Standard width — click to fill the screen"
      }
      className={cn(
        "flex items-center gap-1.5 rounded-md px-1.5 py-1 transition-colors hover:bg-surface-2",
        wide ? "text-accent" : "text-ink-3",
      )}
    >
      <Icon size={12} strokeWidth={2} />
      <span>{wide ? "Wide" : "Standard"}</span>
    </button>
  );
}
```

- [ ] **Step 6: Put the toggle in the sidebar footer**

In `src/App.tsx`, add the import alongside the other component imports near the top:

```tsx
import WideModeToggle from "@/components/WideModeToggle";
```

The sidebar footer currently reads:

```tsx
        <div className="px-6 py-3 hairline-t text-[11px] text-ink-2 shrink-0">
          Harris Budget v{__APP_VERSION__}
        </div>
```

Replace with:

```tsx
        <div className="px-6 py-3 hairline-t text-[11px] text-ink-2 shrink-0 flex items-center justify-between gap-2">
          <span>Harris Budget v{__APP_VERSION__}</span>
          <WideModeToggle />
        </div>
```

The sidebar is `hidden md:flex`, so this control never renders on the mobile web UI that shares this SPA. That is the intended scoping — do not add a separate mobile affordance.

- [ ] **Step 7: Typecheck**

Run from `C:/Users/Steven/ynabhelper-ui`:

```
npx tsc --noEmit
```

Expected: clean. A failure here is almost certainly the `@/lib/useWideMode` import path or a missing default export on the toggle.

- [ ] **Step 8: Build**

```
npm run build
```

Expected: succeeds. A pre-existing chunk-size warning about the main bundle exceeding 500 kB is normal and unrelated — do not try to fix it.

- [ ] **Step 9: Confirm the CSS actually emitted**

Tailwind only emits what it sees, and a hand-written rule inside `@layer components` is easy to get silently dropped by a typo in the block. Prove it survived the build:

```bash
grep -c "page-container" dist/assets/*.css
```

Expected: at least 2 matches (the base rule and the `html.wide` override). Zero means the rule never made it into the bundle and the toggle will appear to do nothing.

- [ ] **Step 10: Commit**

```bash
git add src/lib/useWideMode.ts src/components/WideModeToggle.tsx src/components/PageShell.tsx src/styles.css src/App.tsx
git commit -m "feat(ui): wide display mode toggle for large monitors"
```

---

## Task 2: Window size persistence

Without this the toggle looks broken: the window opens at 1280×800 every launch regardless of screen, so wide mode has no room to do anything until the window is manually maximized.

**Files:**
- Modify: `src-tauri/Cargo.toml` (the `[dependencies]` block)
- Modify: `src-tauri/src/lib.rs` (line 24, `tauri::Builder::default()`)
- Modify: `src-tauri/tauri.conf.json` (the `windows` array)

**Interfaces:**
- Consumes: nothing from Task 1 — these are independent and could ship in either order
- Produces: no exports; a behavioural change only

- [ ] **Step 1: Add the dependency**

In `src-tauri/Cargo.toml`, add to the `[dependencies]` block:

```toml
tauri-plugin-window-state = "2"
```

This is the first-party Tauri plugin. It is preferred over hand-rolled bounds storage because it already handles multi-monitor coordinates, recovering a window that would restore off-screen, and maximized state — all easy to get subtly wrong.

- [ ] **Step 2: Register the plugin**

In `src-tauri/src/lib.rs`, the builder currently starts at line 24:

```rust
    tauri::Builder::default()
        .setup(|app| {
```

Insert the plugin registration immediately after `default()`:

```rust
    tauri::Builder::default()
        .plugin(tauri_plugin_window_state::Builder::default().build())
        .setup(|app| {
```

There are no other `.plugin(...)` calls in this file — this is the first.

- [ ] **Step 3: Raise the default window size**

In `src-tauri/tauri.conf.json`, the `windows` array's first entry currently has:

```json
        "width": 1280,
        "height": 800,
        "minWidth": 1024,
        "minHeight": 640,
```

Change only the first two:

```json
        "width": 1800,
        "height": 1100,
        "minWidth": 1024,
        "minHeight": 640,
```

`minWidth` and `minHeight` stay as they are — the layout isn't designed below that, and a restored-too-small window should still be floored.

These defaults only apply on a fresh install before any state is saved. Once the plugin has written state, it wins.

- [ ] **Step 4: Build the Rust side**

```
npm run build
```

then

```
npm run tauri build
```

Expected: both succeed. The Rust build takes several minutes and will download and compile the new crate. If the plugin fails to resolve, check that the Tauri core version in `Cargo.toml` is `2.0` — the plugin's major version must match Tauri's.

- [ ] **Step 5: Commit**

```bash
git add src-tauri/Cargo.toml src-tauri/Cargo.lock src-tauri/src/lib.rs src-tauri/tauri.conf.json
git commit -m "feat(ui): remember window size and position across launches"
```

Include `Cargo.lock` — it pins the resolved plugin version and belongs in version control for an application.

---

## Task 3: Deploy and confirm on the real monitor

Everything above is invisible until the built binary replaces the installed one. This task is deliberately separate because it touches the operator's running application, and because the only meaningful verification for a layout change is looking at it.

**Files:** none — deployment only.

- [ ] **Step 1: Confirm the build is current**

```bash
ls -la src-tauri/target/release/ynabhelper-ui.exe
```

If its timestamp predates your Task 2 commit, run `npm run tauri build` again.

- [ ] **Step 2: Back up the installed binary**

```bash
cp "C:/Users/Steven/AppData/Local/Harris Budget/ynabhelper-ui.exe" \
   "C:/Users/Steven/AppData/Local/Harris Budget/ynabhelper-ui.exe.bak-$(date +%Y%m%d-%H%M)"
```

- [ ] **Step 3: Close the app, swap the binary, relaunch**

The running app holds a lock on the exe, so it must be closed first.

```powershell
Stop-Process -Name "ynabhelper-ui" -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3
Copy-Item "C:\Users\Steven\ynabhelper-ui\src-tauri\target\release\ynabhelper-ui.exe" `
          "C:\Users\Steven\AppData\Local\Harris Budget\ynabhelper-ui.exe" -Force
Start-Process "C:\Users\Steven\AppData\Local\Harris Budget\ynabhelper-ui.exe"
```

- [ ] **Step 4: Verify by eye — the four things nothing else can catch**

Ask the operator to confirm, or confirm yourself if you can see the screen:

1. **No narrow flash on launch.** With wide mode on, quit fully and reopen. The layout should be wide from the first frame. A visible snap means the module-load read in Step 2 of Task 1 regressed into an effect.
2. **The toggle persists.** Turn it on, fully quit, relaunch. It should still be on and still read "Wide".
3. **The window reopens where you left it.** Move and resize it, quit, relaunch.
4. **Content actually fills the width.** Open Transactions — a wide table — and confirm the rows span the window rather than sitting in a centred column. Then open Budget and confirm the cards don't look stranded.

- [ ] **Step 5: Report the outcome**

State plainly which of the four checks passed and which didn't. A layout change that typechecks and builds can still be wrong on screen, and that is the only failure mode that matters here.

---

## Rollback

Each task reverts independently.

- **Task 1** is JS and CSS only: `git revert` the commit and rebuild the front end. No Rust rebuild needed.
- **Task 2** touches Rust: `git revert` the commit, then `npm run tauri build` and swap the exe again. The plugin also leaves a small state file in the app's data directory; deleting it restores first-launch defaults.
- **Task 3** is reversed by copying the `.bak-*` binary from Step 2 back over the installed one.

If wide mode misbehaves but the window persistence is fine, revert Task 1 alone — they share no code.
