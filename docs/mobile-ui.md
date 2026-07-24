# Mobile Web UI — Tailscale + phone setup runbook

*Task 10 of the Mobile Web UI SDD plan (`.superpowers/sdd/2026-07-24-mobile-web-ui/`).
Everything upstream (bot serving `webui/`, per-user token auth, the SPA's
http mode + mobile shell + responsive pass) is done as of Tasks 1–9. This
doc covers deploying the built bundle and getting it onto Steven's and
Allison's phones over Tailscale.*

## What this is

The bot (`bot/http_api.py`) already serves the built `ynabhelper-ui` SPA as
static files at `webui/`, plus the same `/q/{name}` read-only query API and
write endpoints the desktop Tauri app uses. The SPA bundle detects it's
running in a browser (not inside Tauri) and switches to "http mode": it
calls those HTTP endpoints directly instead of Tauri's IPC bridge, and
`TokenGate` prompts for a bearer token on first load (stored client-side
after that; `main.tsx` clears it and re-prompts if any query 401s).

The server only binds `127.0.0.1:8765` — it is never exposed directly to
the internet or LAN. Tailscale is the only thing allowed to front it.

## 1. Deploy the built bundle

Run this whenever `ynabhelper-ui` changes and you want phones to see the
update:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
C:\Users\Steven\ynabhelper\scripts\deploy_webui.ps1
```

The script builds `ynabhelper-ui` (`npm run build`) and copies `dist/*`
into `ynabhelper/webui/`. It is a **static file refresh** — no bot restart
needed; the next page load (or hard refresh) picks up the new bundle.

**Deviation from the original plan:** a protection hook on this machine
blocks `Remove-Item webui\*`, so the script does not clear `webui/` first —
it overwrites in place with `Copy-Item -Force`. Any stale hashed asset file
from a previous build (e.g. an old `assets/index-<hash>.js` the new
`index.html` no longer references) is left behind; this is harmless, since
`index.html` only ever points at the current build's hashes and nothing
enumerates the assets directory. `webui/.gitkeep` is untouched regardless,
since `Copy-Item` only overwrites files that exist in the source `dist/`.

## 2. Install Tailscale (one-time)

1. Install Tailscale on the desktop (Windows) and on both phones (iOS/
   Android), signing in to the same tailnet on all three devices.
2. On the desktop, confirm its MagicDNS name (Tailscale admin console, or
   `tailscale status` — looks like `desktop-name.tailXXXX.ts.net`).
3. On the desktop, front the bot's loopback API with Tailscale's HTTPS
   proxy:

   ```powershell
   tailscale serve --bg --https=443 127.0.0.1:8765
   ```

   This makes `https://<desktop-magicdns-name>/` reachable from any device
   on the tailnet, terminating TLS via Tailscale and forwarding to the
   bot's loopback port. It survives desktop reboots once backgrounded
   (`--bg`); re-run the command if `tailscale serve status` shows nothing
   after a Tailscale upgrade.

   **Never port-forward 8765 on the router.** The server binds loopback
   only and has no auth on its static asset routes (see "Auth" below) —
   Tailscale's tailnet membership is the only access control. Exposing the
   port any other way (router port-forward, `0.0.0.0` bind, ngrok, etc.)
   would put the write endpoints on the open internet.

## 3. Phone setup (once per phone)

1. Open the Tailscale app on the phone and confirm it's connected to the
   same tailnet.
2. In a mobile browser, go to `https://<desktop-magicdns-name>/`.
3. `TokenGate` will prompt for a token. Paste the per-user token:
   - **Steven**: the value in `ui_api_token.txt` at the repo root (the
     legacy single-user token; always maps to `"steven"`).
   - **Allison**: `60bf1vhTFLMlgzUAjvVtLIw3J4UJxH6EzcJZwheYrQE` (generated
     2026-07-24, stored in `ui_api_tokens.json` at the repo root as
     `{"<token>": "allison"}`).
     ⚠ This token is recorded in this repo's git history (this file) —
     if this repo is ever pushed, shared, or given a remote, rotate the
     token (regenerate, update `ui_api_tokens.json`, restart the bot)
     before doing so.
4. Once the token is accepted, use the browser's "Add to Home Screen" (iOS
   Safari: share sheet → Add to Home Screen; Android Chrome: menu → Add to
   Home screen) so it launches like an app, full-screen, without browser
   chrome.

**The bot must be restarted to pick up a newly-added `ui_api_tokens.json`
entry** — token maps are loaded once at startup (`_load_token_map` in
`bot/http_api.py`). Adding Allison's token above requires a restart before
her phone's paste will succeed; the running bot process (the
`YNAB-Helper-Bot` scheduled task) picks this up on its next controller-
initiated restart.

## Auth model (for reference)

- `GET /assets/*` and the SPA fallback (`GET /{path}` → `webui/index.html`)
  are unauthenticated — the built JS/CSS/HTML bundle isn't secret, since
  every data call it makes is separately gated.
- `POST /q/{name}`, `POST /categorize`, `POST /envelope/move`,
  `POST /budget/set`, and `GET /categories` all require the `X-API-Token`
  header, checked against the combined token map (`ui_api_token.txt` +
  `ui_api_tokens.json`). Writes stamp `filed_by` with whichever username
  the token mapped to, so Allison's edits are attributed to her, not
  Steven.

## Gotchas

- **SPA catch-all route shape matching.** The catch-all
  (`GET /{full_path:path}` in `bot/http_api.py`) decides whether a path is
  a real (missing/mistyped) API call — and should 404 — or an SPA
  client-side route — and should serve `index.html` — by comparing the
  request's path *segment shape* (segment count + which segments are
  static vs. `{param}`) against every route already registered on the
  FastAPI app, computed from `app.routes` at startup. This is what lets
  `/budget` fall through to the SPA while `/budget/set` and `/q/anything`
  still 404 instead of silently serving the shell. If a future SPA
  client-side route is ever added whose path shape collides with an
  existing API route (same segment count, same static segments — e.g. a
  hypothetical client route literally named `/q/{something}`), it will be
  swallowed by the 404 guard instead of reaching the SPA. Any such new
  client route needs a name that doesn't collide with an API route shape;
  there's no automatic detection of this at build time.
- **Unported panels 404 in http mode by design.** `q_seasonal_funds` is a
  deliberate stub (returns `[]`) and Reconciler/Investments have no
  `/q/*` entries in `bot/webui_queries.py`'s `REGISTRY` at all — the
  Seasonal, Reconciler, and Investments panels are desktop-only for now
  and aren't reachable from the mobile nav. A `/q/q_reconciler_*` or
  `/q/q_investments_*` call from a phone will 404; this is expected, not a
  bug, until those panels get a mobile-web port.
- **Per-user tokens live in two files, deliberately.** `ui_api_token.txt`
  (Steven, legacy, single token, no username key — always `"steven"`) and
  `ui_api_tokens.json` (everyone else, `{"<token>": "<username>"}` map —
  currently just Allison). Both are repo-root files, both gitignored, both
  read once at bot startup and merged into one token→username map.
