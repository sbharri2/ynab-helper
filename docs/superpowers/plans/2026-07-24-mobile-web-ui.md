# Mobile Web UI (MVP) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the existing React UI from the bot's FastAPI server so Steven's and Allison's phones (over Tailscale) get a mobile-usable Today / Budget / Transactions / Needs-attention experience against live data.

**Architecture:** The desktop UI is a React SPA whose only native dependency is `invoke("q_*", args)` (reads, Rust/rusqlite) and `invoke("api_post"| "api_get")` (writes, proxied to the bot's FastAPI on 127.0.0.1:8765). We add (1) a Python **query registry** exposing the same `q_*` names over `POST /q/{name}` by porting the SQL verbatim from `ynabhelper-ui/src-tauri/src/commands.rs`, (2) an **http transport mode** in the SPA's data layer so the identical bundle runs in a phone browser, (3) FastAPI serving the built bundle, (4) per-user tokens so `filed_by` is honest. Phones reach it via Tailscale only — the server keeps its 127.0.0.1 bind and `tailscale serve` fronts it with HTTPS.

**Tech Stack:** FastAPI (existing `bot/http_api.py`), sqlite3, pytest; React 18 + TanStack Query + Vite (existing `ynabhelper-ui`); Tailscale.

## Global Constraints

- Bot repo: `C:\Users\Steven\ynabhelper` (branch `mvp1-implementation`); UI repo: `C:\Users\Steven\ynabhelper-ui` (branch `master`). Commit per task, **never push**.
- Live DB: `C:\Users\Steven\ynabhelper\ynab_helper.db`. Tests run against a **copy** (fixture below), never the live file.
- The bot stays the single writer. New endpoints are read-only except where they call existing write paths.
- Pydantic body models at **module scope** in `http_api.py` (closure trap — see MEMORY).
- Bot restarts ONLY via `Stop-ScheduledTask`/`Start-ScheduledTask -TaskName "YNAB-Helper-Bot"`. Code goes live only on restart.
- Server keeps binding `127.0.0.1:8765`. No internet exposure, ever. Phone access = Tailscale (Task 10).
- Response shapes MUST serialize to the TypeScript interfaces in `ynabhelper-ui/src/lib/types.ts` — that file is the contract; field names are snake_case exactly as there.
- SQL for each ported query is copied **verbatim** from `ynabhelper-ui/src-tauri/src/commands.rs` (function name + line given per task). Only syntax-level changes allowed (rusqlite `?1` → sqlite3 `?`, params tuple).
- Run `python -m pyflakes` on touched Python files and `npx tsc --noEmit` on the UI before each commit.

## File Structure

**Bot repo (`ynabhelper/`):**
- Create: `bot/webui_queries.py` — the `q_*` registry: pure functions `(db_path, **args) -> JSON-serializable`, one per ported query, plus `REGISTRY: dict[str, callable]`.
- Modify: `bot/http_api.py` — token→user map, `POST /q/{name}` dispatch route, static-file mount for the SPA bundle.
- Create: `tests/test_webui_queries.py` — shape/value tests against a fixture DB.
- Create: `webui/` — deploy target for the built SPA bundle (gitignored; add `webui/.gitkeep`).
- Create: `docs/mobile-ui.md` — Tailscale + phone setup runbook.

**UI repo (`ynabhelper-ui/`):**
- Create: `src/lib/httpTransport.ts` — fetch-based `httpQuery` / `httpPost` with token handling.
- Modify: `src/lib/db.ts` — 3-way mode: `tauri | http | mock`.
- Modify: `src/lib/api.ts` — writes go over fetch in http mode.
- Create: `src/components/TokenGate.tsx` — one-time token entry, localStorage.
- Modify: `src/App.tsx` — viewport-aware shell: bottom tab nav on small screens, mobile nav subset, TokenGate wrap in http mode.
- Modify: `index.html` — viewport meta.
- Modify: `src/pages/Budget.tsx`, `src/pages/Transactions.tsx` — responsive card layouts under `sm:`.

## MVP query port list (everything else 404s and its UI feature simply doesn't render on mobile)

`q_categories`, `q_category_groups`, `q_month_categories`, `q_category_avg_activity`, `q_cash_trace`, `q_cc_balance_summary`, `q_transactions`, `q_inbox`, `q_income_sources`, `q_ready_to_assign`, `q_seasonal_funds` (deliberate `[]` stub in MVP — Budget's review derivation already tolerates an empty list; the Seasonal page is not in the mobile nav).

---

### Task 1: Per-user tokens (identity for `filed_by`)

**Files:**
- Modify: `bot/http_api.py` (token check `_require_token`, and the hardcoded `filed_by = 'steven'` at ~line 438)
- Create: `ui_api_tokens.json` (NOT committed — add to `.gitignore`)
- Test: `tests/test_webui_queries.py::test_token_identity`

**Interfaces:**
- Produces: `_require_token` FastAPI dependency now returns the username (`str`); route handlers that stamp `filed_by` take `user: str = Depends(_require_token)`.
- `ui_api_tokens.json` shape: `{"<token>": "steven", "<token2>": "allison"}`. Legacy `ui_api_token.txt` continues to work and maps to `"steven"` (the desktop keeps working unchanged).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_webui_queries.py
import json
from fastapi.testclient import TestClient

def make_app(tmp_path, db_path):
    (tmp_path / "ui_api_token.txt").write_text("legacy-tok")
    (tmp_path / "ui_api_tokens.json").write_text(
        json.dumps({"allison-tok": "allison"}))
    from bot import http_api
    return http_api.build_app(db_path=db_path, token_dir=tmp_path)

def test_token_identity(tmp_path, fixture_db):
    app = make_app(tmp_path, fixture_db)
    c = TestClient(app)
    assert c.get("/categories",
                 headers={"x-api-token": "legacy-tok"}).status_code == 200
    assert c.get("/categories",
                 headers={"x-api-token": "allison-tok"}).status_code == 200
    assert c.get("/categories",
                 headers={"x-api-token": "wrong"}).status_code == 401
```

(`fixture_db` fixture: copies `ynab_helper.db` schema + a handful of rows into `tmp_path/test.db`; see Task 2 Step 1 where it is defined once for the module. If `build_app` doesn't currently accept `token_dir`, add that parameter with default = repo root — read the current signature first and keep existing call sites working.)

- [ ] **Step 2: Run it, verify it fails** — `python -m pytest tests/test_webui_queries.py::test_token_identity -v` → FAIL (unknown `token_dir` kwarg or 401 for allison-tok).

- [ ] **Step 3: Implement** — in `http_api.py`: load both token files at app build; `_require_token(x_api_token: str = Header(...)) -> str` returns the mapped username or raises 401. Replace `filed_by = 'steven'` hardcode with the dependency's return value.

- [ ] **Step 4: Run test, verify pass.** Also run the full existing suite: `python -m pytest tests/ -q` (5 known pre-existing failures in test_amazon_parser/test_conversation/test_ynab_client are not yours).

- [ ] **Step 5: Commit** — `git commit -m "feat: per-user API tokens; filed_by from token identity"`.

### Task 2: Query registry + `/q/{name}` route (with the two trivial queries)

**Files:**
- Create: `bot/webui_queries.py`
- Modify: `bot/http_api.py`
- Test: `tests/test_webui_queries.py`

**Interfaces:**
- Produces: `webui_queries.REGISTRY: dict[str, Callable]`; each callable is `f(db_path, **args)` returning JSON-serializable data. Route: `POST /q/{name}` with JSON body = args dict (may be `{}`), token-gated, returns the callable's result; unknown name → 404. **camelCase keys in the body are converted to snake_case by the route** (the SPA sends `{"month": ...}` and `{"accountId": ...}` style — mirror `db.ts`'s conversions by accepting both: convert `[A-Z]` → `_[a-z]` on every key).
- First two registry entries: `q_categories` (port from `commands.rs:109`, response = `Category[]` in `types.ts`) and `q_category_groups` (`commands.rs:89`, response = `CategoryGroup[]`).

- [ ] **Step 1: Write the module fixture + failing tests**

```python
# top of tests/test_webui_queries.py
import shutil, sqlite3, pytest
from pathlib import Path

LIVE = Path(r"C:\Users\Steven\ynabhelper\ynab_helper.db")

@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory):
    p = tmp_path_factory.mktemp("db") / "test.db"
    shutil.copyfile(LIVE, p)   # full copy: read-only tests, real shapes
    return str(p)

def test_registry_q_categories(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_categories"](fixture_db)
    assert rows and {"id", "group_id", "group_name", "name",
                     "is_spending", "hidden"} <= set(rows[0])

def test_q_route(tmp_path, fixture_db):
    app = make_app(tmp_path, fixture_db)
    c = TestClient(app)
    h = {"x-api-token": "legacy-tok"}
    assert c.post("/q/q_categories", json={}, headers=h).status_code == 200
    assert c.post("/q/nope", json={}, headers=h).status_code == 404
    assert c.post("/q/q_categories", json={}).status_code in (401, 422)
```

- [ ] **Step 2: Run, verify both fail** (no module / no route).
- [ ] **Step 3: Implement** `webui_queries.py` (connect with `sqlite3.Row`, port the two SELECTs verbatim from `commands.rs:89-140`) and the dispatch route in `http_api.py`:

```python
@app.post("/q/{name}", dependencies=[Depends(_require_token)])
def q_dispatch(name: str, body: dict[str, Any] | None = None):
    fn = webui_queries.REGISTRY.get(name)
    if fn is None:
        raise HTTPException(404, f"unknown query {name}")
    args = {_snake(k): v for k, v in (body or {}).items() if v is not None}
    return fn(db_path, **args)
```

- [ ] **Step 4: Tests pass.**  - [ ] **Step 5: Commit** — `"feat: /q/{name} query dispatch + registry (categories, groups)"`.

### Task 3: Port the mechanical SQL queries

**Files:** Modify `bot/webui_queries.py`; tests appended to `tests/test_webui_queries.py`.

**Interfaces (name → verbatim SQL source → response contract in `types.ts`):**
- `q_month_categories(db, month)` → `commands.rs:143-190` → `MonthCategoryRow[]`
- `q_category_avg_activity(db, month, months=6)` → `commands.rs:2686-2747` → `CategoryAvgRow[]` (check the Rust arg names at line 2686 and mirror them exactly)
- `q_cash_trace(db, months)` → `commands.rs:2496-2573` (the month-window walk is plain arithmetic — port the loop as written) → `CashTraceRow[]`
- `q_cc_balance_summary(db)` → `commands.rs:2575-2624` → `CcSummary`
- `q_transactions(db, account_id=None, category_id=None, payee_search=None, since=None, until=None, limit=None, offset=None)` → `commands.rs:192-287` **including** the `pending_txn` join with both key forms and `a.on_budget`/`t.is_split` columns → `Transaction[]`
- `q_inbox(db)` → `commands.rs:2864-end of fn` → `InboxRow[]`

- [ ] **Step 1: Failing shape tests** — one per query, same pattern:

```python
def test_q_transactions_shape(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_transactions"](fixture_db, since="2026-07-01",
                                      until="2026-07-31", limit=50)
    assert rows
    need = {"id", "account_name", "posted_date", "amount_cents", "payee",
            "category_name", "pending_id", "decision_state",
            "asked_in_chat", "account_on_budget", "is_split"}
    assert need <= set(rows[0])

def test_q_month_categories_available_identity(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_month_categories"](fixture_db, month="2026-07")
    r = next(x for x in rows if x["category_name"] == "Groceries")
    assert isinstance(r["available_cents"], int)
```

- [ ] **Step 2: Run, all fail.**
- [ ] **Step 3: Port each function.** Open `commands.rs` at the given lines, copy the SQL string, convert placeholders, return `[dict(r) for r in rows]` with the exact field names the Rust struct mapping uses (cross-check every field against the `types.ts` interface before moving on). Register each in `REGISTRY`.
- [ ] **Step 4: Tests pass.**
- [ ] **Step 5: One cross-check against the live app** (read-only): `python -c` invoking `q_cash_trace(LIVE_DB, months=6)` and compare the last row's `end_cash_cents` to the RTA card's cash figure in the running desktop app. Must match to the cent.
- [ ] **Step 6: Commit** — `"feat: port register/budget/cash read queries to webui registry"`.

### Task 4: Port the income engine (`q_income_sources` + `q_ready_to_assign`)

The one genuinely intricate port. Source: `commands.rs:2018-2235` (sources: 6-month grouping, cadence classification, `income_source_override` merge, retired-key filter) and `commands.rs:2237-2494` (RTA: actuals map, prediction walk with the ±3-day match, unmatched-actual "received" rows added 2026-07-24, misc income, assigned, cash, in-flight detector). Also port the `next_predicted_date` helper the walk calls (search `fn next_predicted_date` in the same file).

**Files:** Modify `bot/webui_queries.py`; tests in `tests/test_webui_queries.py`.

**Interfaces:** `q_income_sources(db)` → `IncomeSource[]`; `q_ready_to_assign(db, month)` → `ReadyToAssign` (`types.ts:243-262` — includes `paychecks`, `any_overdue`, `in_flight_cents`).

- [ ] **Step 1: Failing golden tests** (values from the live copy — they pin the port to the Rust behavior):

```python
def test_income_sources_golden(fixture_db):
    from bot.webui_queries import REGISTRY
    srcs = {s["payee_key"]: s for s in REGISTRY["q_income_sources"](fixture_db)}
    assert "ACH O'BRIEN/ATKINS A" in srcs
    assert srcs["ACH O'BRIEN/ATKINS A"]["last_seen"] == "2026-07-14"
    assert "ACH ACTALENT, INC." in srcs          # manual weekly override
    assert srcs["ACH ACTALENT, INC."]["cadence"] == "weekly"

def test_rta_matches_identity(fixture_db):
    from bot.webui_queries import REGISTRY
    rta = REGISTRY["q_ready_to_assign"](fixture_db, month="2026-07")
    assert rta["ready_to_assign_cents"] == rta["cash_cents"] - rta["available_cents"]
    dates = [p["expected_date"] for p in rta["paychecks"]
             if p["source_payee"] == "ACH O'BRIEN/ATKINS A"]
    assert "2026-07-14" in " ".join(
        (p.get("actual_date") or p["expected_date"]) for p in rta["paychecks"])
```

(If the fixture copy postdates data changes, update the golden values from the live desktop app first — the assertion *style* is the requirement.)

- [ ] **Step 2: Run, fail.**  - [ ] **Step 3: Port**, keeping the Rust structure: candidates query → cadence classification (delta arms 13..=15 biweekly-vs-semi via calendar clustering, 16..=17 semi, 28..=32 monthly, else None) → overrides append/retire → walk. Use `datetime.date` throughout; format `%Y-%m-%d`.
- [ ] **Step 4: Tests pass.**  - [ ] **Step 5: Live cross-check** — run against the live DB, diff every top-level field against the desktop RTA card. Any mismatch is a port bug; fix before committing.
- [ ] **Step 6: Commit** — `"feat: port income sources + ready-to-assign to webui registry"`.

### Task 5: Seasonal stub + static SPA serving

**Files:** Modify `bot/webui_queries.py` (`q_seasonal_funds` returning `[]`), `bot/http_api.py`, `.gitignore` (+`webui/`), create `webui/.gitkeep`.

**Interfaces:** GET `/` and any non-`/q`, non-API path → `webui/index.html` (SPA fallback); `/assets/*` served from `webui/assets`. Static routes are UNAUTHENTICATED (the bundle is not secret; data routes stay token-gated).

- [ ] **Step 1: Failing test**

```python
def test_spa_serving(tmp_path, fixture_db):
    app = make_app(tmp_path, fixture_db)   # make_app gains webui_dir=tmp_path/"webui"
    (tmp_path / "webui").mkdir()
    (tmp_path / "webui" / "index.html").write_text("<html>hb</html>")
    c = TestClient(app)
    assert c.get("/").text == "<html>hb</html>"
    assert c.get("/budget").text == "<html>hb</html>"      # SPA fallback
    assert c.post("/q/q_seasonal_funds", json={},
                  headers={"x-api-token": "legacy-tok"}).json() == []
```

- [ ] **Step 2: Fail.**  - [ ] **Step 3: Implement** — `StaticFiles` mount for `/assets`, catch-all GET route returning `FileResponse(webui/index.html)` for paths not starting with `/q`, `/categories`, etc. (register it LAST so API routes win).
- [ ] **Step 4: Pass.**  - [ ] **Step 5: Commit** — `"feat: serve SPA bundle + seasonal stub"`.

### Task 6: UI http transport

**Files:** Create `ynabhelper-ui/src/lib/httpTransport.ts`; modify `src/lib/db.ts`, `src/lib/api.ts`.

**Interfaces:**
- `httpTransport.ts` exports `httpQuery<T>(cmd: string, args?: Record<string, unknown>): Promise<T>` → `POST /q/{cmd}` with `{...args}`, header `x-api-token` from `localStorage.getItem("hb.token")`; and `httpPost<T>(endpoint: string, body: object): Promise<T>` → same-origin POST with token header. Both throw `Error("unauthorized")` on 401 (TokenGate listens for it).
- `db.ts` mode: `export const dataSourceMode: "tauri" | "http" | "mock"` — `tauri` if `__TAURI_INTERNALS__`, else `http` if `import.meta.env.PROD`, else `mock`. Every `isTauri ? invokeTauri(cmd, args) : mockQuery(...)` becomes a call through one helper `query<T>(cmd, args, mockKey?)` that switches on the mode (http branch: `httpQuery(cmd, args)` with the SAME snake_case arg objects already built for Tauri — the server's key conversion accepts both).
- `api.ts`: in http mode `apiPost` → `httpPost(endpoint, body)` (no Tauri import).

- [ ] **Step 1: Typecheck-driven change** (UI repo has no unit-test rig; the gates are `npx tsc --noEmit`, `npm run build`, and Step 4's manual check). Write `httpTransport.ts` exactly:

```ts
const base = "";  // same-origin
export async function httpQuery<T>(cmd: string, args: Record<string, unknown> = {}): Promise<T> {
  const res = await fetch(`${base}/q/${cmd}`, {
    method: "POST",
    headers: { "Content-Type": "application/json",
               "x-api-token": localStorage.getItem("hb.token") ?? "" },
    body: JSON.stringify(args),
  });
  if (res.status === 401) throw new Error("unauthorized");
  if (!res.ok) throw new Error(`HTTP ${res.status} — ${await res.text()}`);
  return res.json() as Promise<T>;
}
export async function httpPost<T>(endpoint: string, body: object): Promise<T> {
  const res = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json",
               "x-api-token": localStorage.getItem("hb.token") ?? "" },
    body: JSON.stringify(body),
  });
  if (res.status === 401) throw new Error("unauthorized");
  if (!res.ok) throw new Error(`HTTP ${res.status} — ${await res.text()}`);
  return res.json() as Promise<T>;
}
```

- [ ] **Step 2: Rework `db.ts`** to the 3-way `query` helper; keep each function's existing snake_case arg construction (`q_transactions` camelCase conversion applies ONLY in the tauri branch — http sends snake_case as-is).
- [ ] **Step 3: `api.ts`** http branch in `apiPost`/`apiGet` (http mode: `apiGet(endpoint)` → same-origin GET with token header).
- [ ] **Step 4: Verify** — `npx tsc --noEmit` clean; `npm run build`; copy `dist/*` into `ynabhelper/webui/`; on the DESKTOP browser open `http://127.0.0.1:8765/` (bot restarted with Tasks 1–5), paste token when prompted by console error for now, and confirm the Budget page renders live numbers matching the desktop app.
- [ ] **Step 5: Commit** — `"feat: http data-source mode - SPA runs in a plain browser against the bot API"`.

### Task 7: TokenGate

**Files:** Create `src/components/TokenGate.tsx`; modify `src/App.tsx`.

**Interfaces:** In http mode only, `TokenGate` wraps the router: if no `hb.token` in localStorage (or any query threw `unauthorized`), render a centered card: password input ("Paste your access token"), Save button storing to `localStorage["hb.token"]` then `location.reload()`. Tauri/mock modes render children directly.

- [ ] **Step 1: Implement** (single component, ~60 lines, styled with the existing `card`/`bg-accent` utility classes as used in `NewCategoryModal` — copy its button classes verbatim).
- [ ] **Step 2: Wire into `App.tsx`** around the existing providers. Add a global `queryCache` error listener: on `Error("unauthorized")`, clear `hb.token` and re-render the gate.
- [ ] **Step 3: Verify** — build, redeploy to `webui/`, open in a private browser window: gate appears, wrong token → gate returns, right token → app loads.
- [ ] **Step 4: Commit** — `"feat: token gate for browser mode"`.

### Task 8: Mobile shell (bottom nav + viewport)

**Files:** Modify `ynabhelper-ui/index.html`, `src/App.tsx`.

**Interfaces:** `index.html` gets `<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">`. In `App.tsx`: the existing sidebar hides below `md:` (`hidden md:flex` on its container); a new bottom tab bar (`fixed bottom-0 inset-x-0 md:hidden`, safe-area padding `pb-[env(safe-area-inset-bottom)]`) with exactly four tabs → routes: Today `/`, Budget `/budget`, Transactions `/transactions`, Review `/transactions?view=needs-attention`; icons reuse the sidebar's (`CalendarDays`/`Wallet`/`List`/`Inbox` — match whatever the sidebar imports for those routes). Main content gets `pb-16 md:pb-0` so the bar never covers content.

- [ ] **Step 1: Implement** both changes.
- [ ] **Step 2: Verify** — `npx tsc --noEmit`; build; redeploy; in desktop browser devtools set iPhone viewport: sidebar gone, bottom tabs present, all four tabs navigate; desktop viewport unchanged (sidebar back, no tab bar).
- [ ] **Step 3: Commit** — `"feat: mobile shell - viewport meta + bottom tab nav under md"`.

### Task 9: Responsive pass on Budget + Transactions

**Files:** Modify `src/pages/Budget.tsx`, `src/pages/Transactions.tsx`.

**Interfaces:** No data-layer changes. Small screens (`<sm`) get card rows instead of wide tables:
- Transactions: hide the table header on `<sm`; each row renders as a two-line block — line 1: payee (truncate) + amount right-aligned; line 2 (text-xs, ink-2): `date · account · category-or-chip`. Implementation: keep the `<table>`, apply `block sm:table` pattern is fragile — instead render a parallel `<ul className="sm:hidden">` above the `<table className="hidden sm:table">`, both mapping the same `filtered` array and reusing the existing chip/receipt/Ask-household elements.
- Budget: the summary cards grid `grid-cols-4` → `grid-cols-2 sm:grid-cols-4`; the category table gains the same `sm:hidden` card list: name + signal dot, available (colored, right), second line `budgeted → activity`, tapping a card opens the existing `AvailableBreakdownModal` (`setBreakdownFor(r)`); header buttons (`New category`, `Top up month`, `Reconcile month`) wrap via `flex-wrap`.
- Quick buttons (`0`/`avg`) and inline BudgetedCell editing stay desktop-only (`hidden sm:flex` on their container) — on mobile, editing goes through the breakdown modal's existing reduce button (mobile write surface can grow later).

- [ ] **Step 1: Transactions card list.** - [ ] **Step 2: Budget card list + grid fixes.**
- [ ] **Step 3: Verify** — build, redeploy, iPhone viewport: both pages readable with no horizontal scroll; categorize an item from Needs-attention on mobile (suggested-category chip tap) and see it file.
- [ ] **Step 4: Commit** — `"feat: mobile card layouts for Budget + Transactions"`.

### Task 10: Deploy script + Tailscale runbook

**Files:** Create `ynabhelper/scripts/deploy_webui.ps1`, `ynabhelper/docs/mobile-ui.md`.

- [ ] **Step 1: `deploy_webui.ps1`** (pair with `Set-ExecutionPolicy -Scope Process Bypass -Force` per machine policy):

```powershell
Set-Location C:\Users\Steven\ynabhelper-ui
npm run build
if ($LASTEXITCODE -ne 0) { throw "vite build failed" }
Remove-Item C:\Users\Steven\ynabhelper\webui\* -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item dist\* C:\Users\Steven\ynabhelper\webui\ -Recurse -Force
Write-Host "deployed; bundle is picked up immediately (static files, no bot restart)"
```

- [ ] **Step 2: `docs/mobile-ui.md`** — runbook: install Tailscale on desktop + both phones (same tailnet); on desktop run `tailscale serve --bg --https=443 127.0.0.1:8765`; phones browse to `https://<desktop-magicdns-name>/`; paste per-user token (Steven's from `ui_api_token.txt`, Allison's from `ui_api_tokens.json`); add-to-home-screen. Include the warning: never port-forward 8765; the server binds loopback and only Tailscale may front it.
- [ ] **Step 3: Run the deploy script; verify end-to-end on the desktop browser; commit** — `"feat: webui deploy script + mobile runbook"`.

### Task 11: Restart + live smoke test

- [ ] **Step 1:** Restart the bot via the scheduled task. Confirm `Running`.
- [ ] **Step 2:** Desktop browser `http://127.0.0.1:8765/`: token gate → Today shows the same RTA/cash as the desktop app; Budget matches; file one real Needs-attention item end-to-end; confirm `filed_by` in `pending_txn` equals the token's user.
- [ ] **Step 3:** Report to Steven with the Tailscale steps that only he can do (device installs), and what's deferred (Seasonal page, Reconciler, Investments, per-panel ports).

## Self-Review (performed 2026-07-24)

- **Spec coverage:** phones reach live data over Tailscale ✓ (T5 serving, T10 runbook); Today/Budget/Transactions/Needs-attention ✓ (T3/T4 data, T8/T9 UI); Allison identity ✓ (T1); YAGNI respected — unported panels 404 cleanly rather than half-work.
- **Placeholder scan:** every port task names its verbatim SQL source (file:lines) and its response contract (`types.ts`); no TBDs.
- **Type consistency:** transport is `httpQuery(cmd, args)` everywhere; registry callables `(db_path, **args)`; token storage key `hb.token` in T6/T7; deploy dir `ynabhelper/webui/` in T5/T6/T10.
