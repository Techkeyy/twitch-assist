param([string]$Channel = "iszee24", [string]$TokenFile = "C:\Users\HomePC\Desktop\Twitch Access.txt", [int]$Cooldown = 5)
# Dev run: loads the Twitch token into THIS process's environment only (never printed, never written
# to .env/config.yaml), then runs the Hermes gateway in the foreground.
$ErrorActionPreference = "Stop"
$env:HERMES_HOME = "C:\Users\HomePC\AppData\Local\hermes"
$tok = (Get-Content -Raw $TokenFile).Trim()
if ($tok -match '^(?i)oauth:') { $tok = $tok.Substring(6) }
$env:TWITCH_TOKEN = $tok
$env:TWITCH_CHANNEL = $Channel
$env:TWITCH_COOLDOWN_SECONDS = "$Cooldown"
& "$env:HERMES_HOME\bin\hermes.exe" gateway run
