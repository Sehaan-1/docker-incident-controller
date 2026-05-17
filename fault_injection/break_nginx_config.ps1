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

Invoke-Compose exec -T agent sh -c "printf '%s\n' 'server {' '    listen 80;' '    definitely_invalid_directive on;' '}' > /nginx_conf/site.conf"

# Restarting applies the invalid conf.d snippet. A non-zero restart is expected
# when nginx refuses to start with the injected configuration.
try {
    Invoke-Compose restart nginx | Out-Null
} catch {
}

Write-Host "Injected invalid nginx conf.d/site.conf. Nginx should now fail config load."

