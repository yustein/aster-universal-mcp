[CmdletBinding()]
param()
. (Join-Path $PSScriptRoot 'windows-common.ps1')
$manifestPath = Join-Path $script:AsterSourceRoot 'candidate-manifest.json'
$manifest = [ordered]@{ schema_version = 1; files = @(Get-AsterSourceFiles) }
Write-AsterJson $manifestPath $manifest
$identity = Assert-AsterCandidate $manifestPath
[pscustomobject]@{ status = 'candidate_frozen'; source_root = $script:AsterSourceRoot; candidate_id = $identity; source_manifest = $manifestPath; file_count = $manifest.files.Count } | ConvertTo-Json
