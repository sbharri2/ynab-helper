# scripts/deploy_webui.ps1
#
# Builds the ynabhelper-ui SPA and copies the built bundle into
# ynabhelper/webui/, where bot/http_api.py serves it (static /assets mount
# + SPA fallback on GET /{path}, see docs/mobile-ui.md).
#
# Pair with `Set-ExecutionPolicy -Scope Process Bypass -Force` before
# running -- this machine blocks unsigned .ps1 by default.
#
# NOTE (deviation from the original Task 10 plan): a protection hook on
# this machine blocks `Remove-Item webui\*`, so this script does NOT clear
# webui/ before copying. Instead it overwrites in place with
# `Copy-Item -Force`. Stale hashed asset files (e.g. an old
# assets/index-<hash>.js no longer referenced by the new index.html) are
# left behind -- harmless, since index.html only ever points at the
# current hashes and nothing enumerates the assets directory. webui/.gitkeep
# (keeps the directory tracked while its contents stay gitignored) is
# untouched either way since Copy-Item only overwrites files that exist in
# the source dist/.

$ErrorActionPreference = "Stop"

$uiRepo = "C:\Users\Steven\ynabhelper-ui"
$webuiDir = "C:\Users\Steven\ynabhelper\webui"

Set-Location $uiRepo
npm run build
if ($LASTEXITCODE -ne 0) { throw "vite build failed" }

if (-not (Test-Path (Join-Path $uiRepo "dist\index.html"))) {
    throw "build did not produce dist\index.html -- aborting deploy"
}

Copy-Item (Join-Path $uiRepo "dist\*") $webuiDir -Recurse -Force

Write-Host "deployed; bundle is picked up immediately (static files, no bot restart)"
