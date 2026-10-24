[CmdletBinding()]
param([switch]$StopGateway)
$operator = Join-Path $env:ProgramData 'AsterUniversalMCP\operator-windows.ps1'
if (-not (Test-Path -LiteralPath $operator -PathType Leaf)) { throw 'Protected operator is missing. Complete verified setup first.' }
& $operator -Action disable -StopGateway:$StopGateway
exit $LASTEXITCODE
