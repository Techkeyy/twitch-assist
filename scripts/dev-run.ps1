param(
  [Parameter(Mandatory = $true)][string]$Channel,
  [Parameter(Mandatory = $true)][string]$TokenFile,
  [int]$Cooldown = 5
)
# Dev run: loads the Twitch token into THIS process's environment only (never printed, never written
# to .env/config.yaml), then runs the Hermes gateway in the foreground.
# Usage: .\scripts\dev-run.ps1 -Channel yourchannel -TokenFile C:\path\to\token.txt
$ErrorActionPreference = "Stop"
if (-not $env:HERMES_HOME) { $env:HERMES_HOME = Join-Path $env:LOCALAPPDATA "hermes" }
$tok = (Get-Content -Raw $TokenFile).Trim()
if ($tok -match '^(?i)oauth:') { $tok = $tok.Substring(6) }
$env:TWITCH_TOKEN = $tok
$env:TWITCH_CHANNEL = $Channel
$env:TWITCH_COOLDOWN_SECONDS = "$Cooldown"
& "$env:HERMES_HOME\bin\hermes.exe" gateway run
