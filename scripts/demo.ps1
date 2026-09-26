# End-to-end demo that regenerates everything in evidence/.
#   1. REAL LLM discovery for both tasks (needs ANTHROPIC_API_KEY or GEMINI_API_KEY in env or .env)
#   2. Replay scenario matrix (happy path, business outcomes, recoveries, hard failure,
#      second tenant, irreversible approval, supervisor handoff with the simulated operator)
# Usage: .\scripts\demo.ps1            (full; planner auto-detected from .env keys)
#        .\scripts\demo.ps1 -Planner gemini
#        .\scripts\demo.ps1 -SkipDiscovery   (replays only; no API key needed)
param([switch]$SkipDiscovery, [ValidateSet('auto','claude','gemini')][string]$Planner = 'auto')
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = ".venv\Scripts\python.exe"

if (-not $SkipDiscovery) {
    New-Item -ItemType Directory -Force evidence\discovery | Out-Null
    Write-Host "== Discovery 1: savings balance (LLM)"
    & $py -m cua discover --task tasks\get_savings_balance.yaml --planner $Planner --out evidence\discovery
    if ($LASTEXITCODE -ne 0) { throw "discovery 1 failed; see evidence\discovery" }

    Write-Host "== Discovery 2: open sub-account (LLM; irreversible post approved by the simulated operator)"
    $sim = Start-Process $py -ArgumentList "scripts\sim_operator.py","--approve","--timeout","600" -PassThru -NoNewWindow
    try {
        & $py -m cua discover --task tasks\open_sub_account.yaml --planner $Planner --out evidence\discovery
    } finally { Stop-Process -Id $sim.Id -Force -ErrorAction SilentlyContinue }
}

New-Item -ItemType Directory -Force evidence\artifacts | Out-Null
Copy-Item capabilities\*.json evidence\artifacts\ -Force
if (Test-Path evidence\replays) { Remove-Item evidence\replays -Recurse -Force }
Write-Host "== Replay scenario matrix"
& $py scripts\scenarios.py --out evidence\replays
Write-Host "Done. See evidence\replays\summary.md"
