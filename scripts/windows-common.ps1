Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:AsterSourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\', '/')

function Assert-AsterAdministrator {
    if ($env:OS -ne 'Windows_NT') { throw 'These helpers require Windows.' }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Open an elevated PowerShell terminal for this deliberately privileged beta deployment.'
    }
}

function Get-AsterStateDirectory {
    if (-not $env:ProgramData) { throw 'ProgramData is not available.' }
    return [IO.Path]::GetFullPath((Join-Path $env:ProgramData 'AsterUniversalMCP'))
}

function Assert-AsterPathEqual([string]$Actual, [string]$Expected, [string]$Label) {
    if (-not $Actual -or -not [IO.Path]::IsPathRooted($Actual) -or
        -not [string]::Equals([IO.Path]::GetFullPath($Actual).TrimEnd('\', '/'),
            [IO.Path]::GetFullPath($Expected).TrimEnd('\', '/'), [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label does not match this deployment."
    }
}

function Write-AsterJson([string]$Path, $Value) {
    $temporary = $Path + '.tmp-' + [guid]::NewGuid().ToString('N')
    try {
        $utf8 = New-Object Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($temporary, (($Value | ConvertTo-Json -Depth 12) + "`n"), $utf8)
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Get-AsterSourceFiles {
    if ((Get-Item -LiteralPath $script:AsterSourceRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'The source root must not be a reparse point.'
    }
    $excludedDirectories = @('.git', '.venv', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'dist', 'build', 'artifacts')
    $queue = New-Object 'Collections.Generic.Queue[string]'
    $queue.Enqueue($script:AsterSourceRoot)
    $result = @()
    while ($queue.Count) {
        $directory = $queue.Dequeue()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            if ($item.PSIsContainer -and $excludedDirectories -contains $item.Name) { continue }
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Reparse points are not allowed in a frozen source candidate: $($item.FullName)"
            }
            if ($item.PSIsContainer) { $queue.Enqueue($item.FullName); continue }
            $relative = $item.FullName.Substring($script:AsterSourceRoot.Length + 1).Replace('\', '/')
            if ($relative -eq 'candidate-manifest.json' -or $relative -eq '.coverage') { continue }
            if ($relative -match '\.(pyc|pyo)$') { continue }
            $result += [pscustomobject]@{ path = $relative; sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant() }
        }
    }
    return @($result | Sort-Object -Property path)
}

function Assert-AsterCandidate([string]$ManifestPath, [string]$ExpectedId = '') {
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw 'Candidate manifest missing. Generate or obtain candidate-manifest.json before setup.'
    }
    $candidateId = (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ExpectedId -and $candidateId -cne $ExpectedId) { throw 'Candidate manifest identity changed.' }
    $manifest = Get-Content -LiteralPath $ManifestPath -Encoding UTF8 -Raw | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or -not $manifest.files -or @($manifest.files).Count -eq 0) {
        throw 'Invalid or empty candidate manifest.'
    }
    $expected = @{}
    foreach ($entry in $manifest.files) {
        $relative = [string]$entry.path
        if (-not $relative -or $relative -match '(^/|\\|(^|/)\.\.?(/|$)|:)' -or
            $relative -eq 'candidate-manifest.json' -or $expected.ContainsKey($relative) -or
            [string]$entry.sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Unsafe or duplicate candidate manifest entry.' }
        $expected[$relative] = [string]$entry.sha256
    }
    $actual = @(Get-AsterSourceFiles)
    if ($actual.Count -ne $expected.Count) { throw 'Source file inventory differs from the frozen candidate.' }
    foreach ($entry in $actual) {
        if (-not $expected.ContainsKey($entry.path) -or $expected[$entry.path] -cne $entry.sha256) {
            throw "Source verification failed: $($entry.path). Freeze and verify a new candidate before restarting."
        }
    }
    return $candidateId
}

function Assert-AsterNoListener([int]$Port) {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    if ($listeners.Count) { throw "Port $Port is already listening. Stop and identify the existing process first." }
}

function Protect-AsterState([string]$Directory) {
    $item = Get-Item -LiteralPath $Directory
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'State directory must not be a reparse point.' }
    $administrators = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $system = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($administrators)
    foreach ($sid in @($administrators, $system)) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Directory -AclObject $acl
    foreach ($child in Get-ChildItem -LiteralPath $Directory -Recurse -Force) {
        if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'State must not contain reparse points.' }
        $childAcl = Get-Acl -LiteralPath $child.FullName
        foreach ($rule in $childAcl.Access) {
            $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            if ($rule.AccessControlType -eq 'Allow' -and $sid -notin @('S-1-5-18', 'S-1-5-32-544')) {
                throw "State item has an unexpected access grant: $($child.FullName). Review its ACL before continuing."
            }
        }
    }
}

function Get-AsterWindowsContext {
    $state = Get-AsterStateDirectory
    $path = Join-Path $state 'gateway-config.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Run setup-windows.ps1 before starting or pairing.' }
    $config = Get-Content -LiteralPath $path -Encoding UTF8 -Raw | ConvertFrom-Json
    if ($config.schema_version -ne 1) { throw 'Unsupported Windows configuration version.' }
    Assert-AsterPathEqual $config.source_root $script:AsterSourceRoot 'Source directory'
    Assert-AsterPathEqual $config.python_executable (Join-Path $script:AsterSourceRoot '.venv\Scripts\python.exe') 'Python environment'
    Assert-AsterPathEqual $config.oauth_db (Join-Path $state 'windows-oauth.sqlite') 'OAuth database'
    Assert-AsterPathEqual $config.admin_clients (Join-Path $state 'admin-clients.json') 'Client allowlist'
    Assert-AsterPathEqual $config.source_manifest (Join-Path $state 'candidate-manifest.json') 'Source manifest'
    Assert-AsterPathEqual $config.runtime_manifest (Join-Path $state 'gateway-runtime.json') 'Runtime manifest'
    if (-not (Test-Path -LiteralPath $config.python_executable -PathType Leaf)) { throw 'Repository Python environment is missing.' }
    $null = Assert-AsterCandidate $config.source_manifest $config.candidate_id
    return [pscustomobject]@{ State = $state; ConfigPath = $path; Config = $config; Python = $config.python_executable; Root = $script:AsterSourceRoot }
}

function Set-AsterWindowsEnvironment($Context) {
    $c = $Context.Config
    $env:ASTER_WINDOWS_CONFIG = $Context.ConfigPath
    $env:ASTER_WINDOWS_PUBLIC_ORIGIN = $c.public_origin
    $env:ASTER_WINDOWS_OAUTH_DB = $c.oauth_db
    $env:ASTER_WINDOWS_ADMIN_CLIENTS = $c.admin_clients
    $env:ASTER_WINDOWS_PORT = [string]$c.port
    $env:ASTER_WINDOWS_RUNTIME_MANIFEST = $c.runtime_manifest
    $env:PYTHONPATH = $Context.Root + ';' + (Join-Path $Context.Root 'platforms\windows')
    $env:PYTHONNOUSERSITE = '1'
}

function Get-AsterVerifiedRuntime($Context) {
    $c = $Context.Config
    if (-not (Test-Path -LiteralPath $c.runtime_manifest -PathType Leaf)) { throw 'Gateway runtime manifest is missing; pairing is blocked.' }
    $runtime = Get-Content -LiteralPath $c.runtime_manifest -Encoding UTF8 -Raw | ConvertFrom-Json
    if ($runtime.schema_version -ne 1 -or [int]$runtime.pid -le 0) { throw 'Invalid gateway runtime manifest.' }
    foreach ($field in @('source_root', 'python_executable', 'oauth_db', 'admin_clients')) {
        Assert-AsterPathEqual ([string]$runtime.$field) ([string]$c.$field) "Runtime $field"
    }
    if ($runtime.public_origin -cne $c.public_origin -or [int]$runtime.port -ne [int]$c.port -or
        $runtime.candidate_id -cne $c.candidate_id) { throw 'Running gateway configuration differs from the frozen deployment.' }
    $process = Get-Process -Id ([int]$runtime.pid) -ErrorAction Stop
    $cim = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + [int]$runtime.pid)
    if (-not $cim -or -not $cim.ExecutablePath) { throw 'Cannot verify the gateway process image.' }
    $expectedImage = [string]$runtime.python_executable
    if ($runtime.PSObject.Properties.Name -contains 'process_executable' -and $runtime.process_executable) {
        $expectedImage = [string]$runtime.process_executable
    }
    Assert-AsterPathEqual $cim.ExecutablePath $expectedImage 'Running process image'
    $manifestStarted = [DateTimeOffset]::Parse([string]$runtime.started_at).UtcDateTime
    if ($process.StartTime.ToUniversalTime() -gt $manifestStarted.AddSeconds(2)) { throw 'Runtime PID has been reused by a newer process.' }
    if ($runtime.PSObject.Properties.Name -notcontains 'process_started_at') { throw 'The runtime record lacks the Windows process creation time.' }
    $recordedCreation = [DateTimeOffset]::Parse([string]$runtime.process_started_at).UtcDateTime
    if ([Math]::Abs(($process.StartTime.ToUniversalTime() - $recordedCreation).TotalSeconds) -gt 1) {
        throw 'The live process creation time does not match the runtime record.'
    }
    if (-not $runtime.elevated) { throw 'This privileged beta deployment requires an elevated gateway.' }
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$c.port) -ErrorAction Stop)
    if (-not $listeners.Count) { throw 'Gateway has no listening socket.' }
    foreach ($listener in $listeners) {
        if ([int]$listener.OwningProcess -ne [int]$runtime.pid -or $listener.LocalAddress -ne '127.0.0.1') {
            throw 'Listener ownership or loopback binding does not match the gateway.'
        }
    }
    if (-not (Test-Path -LiteralPath $c.oauth_db -PathType Leaf)) { throw 'Running gateway OAuth database is missing.' }
    return $runtime
}

function Invoke-AsterPython([string]$Python, [string[]]$Arguments) {
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE." }
}
