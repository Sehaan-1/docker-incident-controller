$ErrorActionPreference = "Stop"

$compose = if ($env:COMPOSE) { $env:COMPOSE } else { "docker compose" }
$composeParts = $compose -split "\s+"
$composeExe = $composeParts[0]
$composeBaseArgs = @()
if ($composeParts.Length -gt 1) {
    $composeBaseArgs = $composeParts[1..($composeParts.Length - 1)]
}

function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & $script:composeExe @script:composeBaseArgs @Args
}

Invoke-Compose exec -T agent python -c "import json; from pathlib import Path; Path('/runtime/flags.json').write_text(json.dumps({'crash_on_start': True}) + '\n', encoding='utf-8')"

# The app reads runtime flags at startup, so restart is the controlled fault edge.
try {
    Invoke-Compose restart app | Out-Null
} catch {
}

Write-Host "Enabled app crash_on_start flag and restarted app. The app should now crash on startup."
