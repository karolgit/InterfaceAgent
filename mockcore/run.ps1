# Build and launch the CoreLink mock app.
# Usage: .\mockcore\run.ps1 [-Tenant heritage|lakeshore]
param([string]$Tenant = "heritage")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$jdk = Get-ChildItem "$root\.tools" -Directory -Filter "jdk-*" -ErrorAction SilentlyContinue | Select-Object -First 1
$bin = if ($jdk) { "$($jdk.FullName)\bin" } else { "" }
$javac = if ($bin) { "$bin\javac.exe" } else { "javac" }
$java = if ($bin) { "$bin\java.exe" } else { "java" }
& $javac -d "$PSScriptRoot\out" (Get-ChildItem "$PSScriptRoot\src\com\corelink\*.java").FullName
if ($LASTEXITCODE -ne 0) { throw "javac failed" }
& $java -cp "$PSScriptRoot\out" com.corelink.Main "--tenant=$Tenant"
