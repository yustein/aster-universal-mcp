[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('allowed', 'revoke', 'disable', 'stop')][string]$Action,
    [string]$ClientId = '',
    [switch]$StopGateway
)
# Installed into protected ProgramData by verified setup. This entrypoint must
# never dot-source repository files, import repository Python, or approve access.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-ProtectedItem([string]$Path, [switch]$Directory, [switch]$RuntimeIdentity) {
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        ([bool]$item.PSIsContainer -ne [bool]$Directory)) { throw "Unexpected protected item type: $Path" }
    $acl = Get-Acl -LiteralPath $Path
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if (-not $RuntimeIdentity -and $owner -notin @('S-1-5-18', 'S-1-5-32-544')) { throw "Unexpected protected item owner: $Path" }
    foreach ($rule in $acl.Access) {
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($rule.AccessControlType -eq 'Allow' -and $sid -notin @('S-1-5-18', 'S-1-5-32-544')) {
            throw "Unexpected protected item access grant: $Path"
        }
    }
}

function Assert-Path([string]$Actual, [string]$Expected, [string]$Label) {
    if (-not $Actual -or -not [IO.Path]::IsPathRooted($Actual) -or
        -not [string]::Equals([IO.Path]::GetFullPath($Actual).TrimEnd('\'),
            [IO.Path]::GetFullPath($Expected).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label differs from the protected deployment."
    }
}

function Read-Allowlist {
    $raw = Get-Content -LiteralPath $script:config.admin_clients -Encoding UTF8 -Raw
    if (-not $raw.TrimStart().StartsWith('[')) { throw 'Administrative allowlist must be a JSON array.' }
    # Windows PowerShell 5.1 preserves a JSON array as one object in a pipeline;
    # wrapping that pipeline in @() would introduce a second array level.
    $values = ConvertFrom-Json -InputObject $raw
    if ($values -isnot [Array]) { throw 'Administrative allowlist must be a JSON array.' }
    foreach ($value in $values) { if ($value -isnot [string]) { throw 'Administrative allowlist must contain strings.' } }
    return ,$values
}

function Write-Allowlist([string[]]$Ids) {
    $destination = [string]$script:config.admin_clients
    $temporary = $destination + '.operator-' + [guid]::NewGuid().ToString('N')
    $backup = $destination + '.operator-backup-' + [guid]::NewGuid().ToString('N')
    $replacementSucceeded = $false
    try {
        $json = ConvertTo-Json -InputObject ([object[]]$Ids) -Compress
        [IO.File]::WriteAllText($temporary, ($json + "`n"), (New-Object Text.UTF8Encoding($false)))
        $acl = Get-Acl -LiteralPath $destination
        Set-Acl -LiteralPath $temporary -AclObject $acl
        # PowerShell 5.1 can bind $null to an empty backup path. A unique file
        # beside the protected destination keeps replacement atomic on NTFS.
        [IO.File]::Replace($temporary, $destination, $backup)
        $replacementSucceeded = $true
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
        # Preserve any backup after an ambiguous replacement failure.
        if ($replacementSucceeded -and (Test-Path -LiteralPath $backup)) {
            Remove-Item -LiteralPath $backup -Force
        }
    }
}

function Stop-VerifiedGateway {
    $runtimePath = [string]$script:config.runtime_manifest
    if (-not (Test-Path -LiteralPath $runtimePath -PathType Leaf)) {
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$script:config.port) -ErrorAction SilentlyContinue)
        if ($listeners.Count) { throw 'Port is occupied but no protected runtime record identifies its owner.' }
        return [pscustomobject]@{ status = 'already_stopped'; process_terminated = $false }
    }
    # This file is written by the elevated gateway, whose user may differ from
    # the installing administrator. Its owner is checked against that process
    # below; its private ACL and canonical parent are still required here.
    Assert-ProtectedItem $runtimePath -RuntimeIdentity
    $runtime = Get-Content -LiteralPath $runtimePath -Encoding UTF8 -Raw | ConvertFrom-Json
    if ($runtime.schema_version -ne 1 -or [int]$runtime.pid -le 0 -or -not $runtime.elevated) { throw 'Invalid protected gateway identity.' }
    foreach ($field in @('source_root', 'python_executable', 'oauth_db', 'admin_clients', 'workspace_root')) {
        Assert-Path ([string]$runtime.$field) ([string]$script:config.$field) "Runtime $field"
    }
    if ($runtime.candidate_id -cne $script:config.candidate_id -or
        $runtime.public_origin -cne $script:config.public_origin -or [int]$runtime.port -ne [int]$script:config.port) {
        throw 'Protected runtime does not match the configured gateway.'
    }
    $gatewayProcess = Get-Process -Id ([int]$runtime.pid) -ErrorAction SilentlyContinue
    if (-not $gatewayProcess) {
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$script:config.port) -ErrorAction SilentlyContinue)
        if ($listeners.Count) { throw 'Another process occupies the gateway port; no process was stopped.' }
        return [pscustomobject]@{ status = 'already_stopped'; process_terminated = $false; recorded_pid = [int]$runtime.pid }
    }
    try {
        # Acquire and retain this process handle before validating its lifetime;
        # Kill() then targets this process rather than a later reuse of its PID.
        $null = $gatewayProcess.Handle
        $created = [DateTimeOffset]::Parse([string]$runtime.process_started_at).UtcDateTime
        if ([Math]::Abs(($gatewayProcess.StartTime.ToUniversalTime() - $created).TotalMilliseconds) -gt 1) {
            throw 'Gateway PID was reused; no process was stopped.'
        }
        $runtimeOwner = (Get-Acl -LiteralPath $runtimePath).GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($runtimeOwner -notin @('S-1-5-18', 'S-1-5-32-544')) {
            $cim = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$runtime.pid)
            $processOwner = Invoke-CimMethod -InputObject $cim -MethodName GetOwnerSid
            if ($processOwner.ReturnValue -ne 0 -or $processOwner.Sid -cne $runtimeOwner) {
                throw 'Runtime record owner does not match the verified gateway process owner.'
            }
        }
        Assert-Path $gatewayProcess.MainModule.FileName ([string]$runtime.process_executable) 'Actual process image'
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$script:config.port) -ErrorAction SilentlyContinue)
        foreach ($listener in $listeners) {
            if ([int]$listener.OwningProcess -ne [int]$runtime.pid -or $listener.LocalAddress -ne '127.0.0.1') {
                throw 'Listener ownership differs from the protected gateway; no process was stopped.'
            }
        }
        $gatewayProcess.Kill()
        if (-not $gatewayProcess.WaitForExit(10000)) { throw 'Gateway termination was not confirmed within 10 seconds.' }
        return [pscustomobject]@{ status = 'stopped'; process_terminated = $true; pid = [int]$runtime.pid }
    } finally { $gatewayProcess.Dispose() }
}

try {
    if ($env:OS -ne 'Windows_NT') { throw 'This protected operator requires Windows.' }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run the protected operator from an elevated PowerShell terminal.'
    }
    if ($StopGateway -and $Action -ne 'disable') { throw '-StopGateway is only valid with -Action disable.' }
    if ($Action -eq 'revoke' -and -not $ClientId) { throw 'Supply the exact client ID with -ClientId.' }
    $state = [IO.Path]::GetFullPath((Join-Path $env:ProgramData 'AsterUniversalMCP'))
    Assert-Path $PSCommandPath (Join-Path $state 'operator-windows.ps1') 'Operator entrypoint'
    Assert-ProtectedItem $state -Directory
    $configPath = Join-Path $state 'gateway-config.json'
    Assert-ProtectedItem $configPath
    $script:config = Get-Content -LiteralPath $configPath -Encoding UTF8 -Raw | ConvertFrom-Json
    if ($script:config.schema_version -ne 1 -or [int]$script:config.port -lt 1024 -or [int]$script:config.port -gt 65535) {
        throw 'Invalid protected configuration.'
    }
    foreach ($item in @(
        @('oauth_db', 'windows-oauth.sqlite'), @('admin_clients', 'admin-clients.json'),
        @('source_manifest', 'candidate-manifest.json'), @('runtime_manifest', 'gateway-runtime.json'),
        @('operator_script', 'operator-windows.ps1'), @('workspace_root', 'workspace'))) {
        Assert-Path ([string]$script:config.($item[0])) (Join-Path $state $item[1]) $item[0]
    }
    if (-not [IO.Path]::IsPathRooted([string]$script:config.source_root)) { throw 'Source root must be absolute.' }
    Assert-Path ([string]$script:config.python_executable) (Join-Path $script:config.source_root '.venv\Scripts\python.exe') 'Configured interpreter'
    foreach ($file in @($script:config.admin_clients, $script:config.source_manifest, $script:config.operator_script)) {
        Assert-ProtectedItem $file
    }
    if ((Get-FileHash -LiteralPath $script:config.operator_script -Algorithm SHA256).Hash.ToLowerInvariant() -cne $script:config.operator_sha256) {
        throw 'Protected operator hash differs from the installed configuration.'
    }
    if ((Get-FileHash -LiteralPath $script:config.source_manifest -Algorithm SHA256).Hash.ToLowerInvariant() -cne $script:config.candidate_id) {
        throw 'Protected source manifest identity changed.'
    }
    $before = Read-Allowlist
    $result = [ordered]@{ action = $Action; status = 'completed'; source_integrity_required = $false; protected_state_verified = $true; checked_at = [DateTimeOffset]::UtcNow.ToString('o') }
    switch ($Action) {
        'allowed' { $result.client_ids = @($before) }
        'revoke' {
            $after = @($before | Where-Object { $_ -cne $ClientId })
            Write-Allowlist -Ids $after
            $result.client_id = $ClientId
            $result.removed = (@($before).Count - $after.Count)
            $result.client_ids = Read-Allowlist
            if ($result.client_ids -ccontains $ClientId) { throw 'Client revocation could not be verified.' }
        }
        'disable' {
            Write-Allowlist -Ids @()
            $result.status = 'disabled'
            $result.removed = @($before).Count
            $result.client_ids = Read-Allowlist
            if (@($result.client_ids).Count) { throw 'Administrative disable could not be verified.' }
            $result.running_jobs_cancelled = $false
            if ($StopGateway) {
                try { $result.gateway = Stop-VerifiedGateway }
                catch {
                    $result.status = 'disabled_gateway_stop_blocked'
                    $result.gateway = @{ status = 'blocked'; reason = $_.Exception.Message }
                    $result | ConvertTo-Json -Depth 8
                    exit 1
                }
            }
        }
        'stop' {
            $result.gateway = Stop-VerifiedGateway
            $result.client_permissions_changed = $false
            $result.running_jobs_cancelled = $false
        }
    }
    $result | ConvertTo-Json -Depth 8
    exit 0
} catch {
    [pscustomobject]@{ action = $Action; status = 'blocked'; reason = $_.Exception.Message; checked_at = [DateTimeOffset]::UtcNow.ToString('o') } | ConvertTo-Json
    exit 1
}
