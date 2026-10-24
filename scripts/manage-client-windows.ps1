[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('clients', 'allowed', 'approve', 'revoke')][string]$Action,
    [string]$ClientId = ''
)
. (Join-Path $PSScriptRoot 'windows-common.ps1')
Assert-AsterAdministrator
$context = Get-AsterWindowsContext
if (-not (Test-Path -LiteralPath $context.Config.oauth_db -PathType Leaf)) { throw 'OAuth database is missing.' }
if ($Action -in @('approve', 'revoke') -and -not $ClientId) { throw 'Supply the exact client ID with -ClientId.' }
if ($Action -eq 'approve') { $null = Get-AsterVerifiedRuntime $context }
$arguments = @((Join-Path $context.Root 'admin_client_allowlist.py'), '--db', $context.Config.oauth_db, '--allowlist', $context.Config.admin_clients, $Action)
if ($Action -in @('approve', 'revoke')) { $arguments += $ClientId }
Invoke-AsterPython $context.Python $arguments
