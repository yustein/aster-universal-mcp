[CmdletBinding()]
param()
. (Join-Path $PSScriptRoot 'windows-common.ps1')
Assert-AsterAdministrator
$context = Get-AsterWindowsContext
$runtime = Get-AsterVerifiedRuntime $context
Set-AsterWindowsEnvironment $context
Write-Host ('Verified gateway PID ' + $runtime.pid + ', candidate ' + $runtime.candidate_id)
Write-Host 'Start a fresh authorization request in Claude before issuing this code.'
Write-Host 'A new code replaces any older code. The authorization request expires after five minutes.'
Invoke-AsterPython $context.Python @((Join-Path $context.Root 'pairing_admin.py'), '--db', $context.Config.oauth_db, 'issue', '--ttl', '900')
Write-Host 'The code lasts up to 15 minutes; the current browser authorization still has its separate five-minute limit.'
