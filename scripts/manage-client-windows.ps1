[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('clients', 'allowed', 'approve', 'revoke')][string]$Action,
    [string]$ClientId = ''
)
if ($Action -in @('allowed', 'revoke')) {
    if ($Action -eq 'revoke' -and -not $ClientId) { throw 'Supply the exact client ID with -ClientId.' }
    $operator = Join-Path $env:ProgramData 'AsterUniversalMCP\operator-windows.ps1'
    if (-not (Test-Path -LiteralPath $operator -PathType Leaf)) { throw 'Protected operator is missing. Complete verified setup first.' }
    & $operator -Action $Action -ClientId $ClientId
    exit $LASTEXITCODE
}
. (Join-Path $PSScriptRoot 'windows-common.ps1')
Assert-AsterAdministrator
$context = Get-AsterWindowsContext
if (-not (Test-Path -LiteralPath $context.Config.oauth_db -PathType Leaf)) { throw 'OAuth database is missing.' }
if ($Action -in @('approve', 'revoke') -and -not $ClientId) { throw 'Supply the exact client ID with -ClientId.' }
if ($Action -eq 'approve') { $null = Get-AsterVerifiedRuntime $context }
$arguments = @((Join-Path $context.Root 'admin_client_allowlist.py'), '--db', $context.Config.oauth_db, '--allowlist', $context.Config.admin_clients, $Action)
if ($Action -in @('approve', 'revoke')) { $arguments += $ClientId }
$result = Invoke-AsterPython $context.Python $arguments
if ($Action -eq 'approve') { Protect-AsterAllowlistOwner }
$result
