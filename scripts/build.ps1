# One-time setup + build. Safe to re-run.
#   - Python 3.11 venv in .venv (via uv; does not touch any system Python)
#   - Portable JDK 21 in .tools/ if no JDK is on PATH
#   - Compiles the CoreLink mock app and the accessibility bridge agent jar
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Python
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Installing uv (user-level Python manager)..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (-not (Test-Path ".venv")) { uv venv --python 3.11 .venv }
uv pip install --python .venv\Scripts\python.exe -r requirements.txt

# Java
$jdk = Get-ChildItem ".tools" -Directory -Filter "jdk-*" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $jdk -and -not (Get-Command javac -ErrorAction SilentlyContinue)) {
    Write-Host "Downloading portable Temurin JDK 21 into .tools ..."
    New-Item -ItemType Directory -Force .tools | Out-Null
    Invoke-WebRequest "https://api.adoptium.net/v3/binary/latest/21/ga/windows/x64/jdk/hotspot/normal/eclipse" -OutFile .tools\jdk21.zip
    Expand-Archive .tools\jdk21.zip -DestinationPath .tools
    Remove-Item .tools\jdk21.zip
    $jdk = Get-ChildItem ".tools" -Directory -Filter "jdk-*" | Select-Object -First 1
}
$bin = if ($jdk) { "$($jdk.FullName)\bin\" } else { "" }

& "${bin}javac" -nowarn -d mockcore\out (Get-ChildItem mockcore\src\com\corelink\*.java).FullName
if ($LASTEXITCODE -ne 0) { throw "mockcore build failed" }
& "${bin}javac" -nowarn -d bridge\out (Get-ChildItem bridge\src\com\cua\bridge\*.java).FullName
if ($LASTEXITCODE -ne 0) { throw "bridge build failed" }
& "${bin}jar" cfm bridge\cua-bridge.jar bridge\manifest.txt -C bridge\out .
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. Add an API key there only if you want to run discovery."
}
Write-Host "Build OK. Try: .venv\Scripts\python.exe -m pytest -q tests"
