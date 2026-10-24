[CmdletBinding()]
param([switch]$Background)
. (Join-Path $PSScriptRoot 'windows-common.ps1')
Assert-AsterAdministrator
$context = Get-AsterWindowsContext
Assert-AsterNoListener ([int]$context.Config.port)
if (Test-Path -LiteralPath $context.Config.runtime_manifest) {
    $previous = Get-Content -LiteralPath $context.Config.runtime_manifest -Encoding UTF8 -Raw | ConvertFrom-Json
    if (Get-Process -Id ([int]$previous.pid) -ErrorAction SilentlyContinue) {
        throw 'The prior runtime manifest still names a live process. Identify and stop it before starting another gateway.'
    }
}
Set-AsterWindowsEnvironment $context
$gateway = Join-Path $context.Root 'platforms\windows\oauth_gateway.py'
if ($Background) {
    $stdout = Join-Path $context.State 'gateway-stdout.log'
    $stderr = Join-Path $context.State 'gateway-stderr.log'
    $process = Start-Process -FilePath $context.Python -ArgumentList ('"' + $gateway + '"') -WorkingDirectory $context.Root -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    $lastReason = 'Gateway did not become ready.'
    try {
        do {
            if ($process.HasExited) { throw "Gateway process exited with code $($process.ExitCode). Inspect $stderr." }
            try {
                $runtime = Get-AsterVerifiedRuntime $context
                [pscustomobject]@{ status = 'running'; runtime = $runtime; stdout_log = $stdout; stderr_log = $stderr } | ConvertTo-Json -Depth 8
                return
            } catch { $lastReason = $_.Exception.Message }
            Start-Sleep -Milliseconds 200
        } while ([DateTime]::UtcNow -lt $deadline)
        throw "Gateway readiness failed: $lastReason Inspect $stderr."
    } catch {
        if (-not $process.HasExited) { & "$env:SystemRoot\System32\taskkill.exe" /PID $process.Id /T /F | Out-Null }
        throw
    }
} else {
    Push-Location $context.Root
    try { Invoke-AsterPython $context.Python @($gateway) } finally { Pop-Location }
}
