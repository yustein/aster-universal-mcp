[CmdletBinding()]
param()
. (Join-Path $PSScriptRoot 'windows-common.ps1')
try {
    Assert-AsterAdministrator
    $context = Get-AsterWindowsContext
    $runtime = Get-AsterVerifiedRuntime $context
    [pscustomobject]@{ status = 'verified_running'; source_verified = $true; listener_verified = $true; config_path = $context.ConfigPath; runtime = $runtime; checked_at = [DateTimeOffset]::UtcNow.ToString('o') } | ConvertTo-Json -Depth 8
} catch {
    [pscustomobject]@{ status = 'blocked'; reason = $_.Exception.Message; checked_at = [DateTimeOffset]::UtcNow.ToString('o') } | ConvertTo-Json
    exit 1
}
