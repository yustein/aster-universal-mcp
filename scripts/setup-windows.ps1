[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PublicOrigin,
    [ValidateRange(1024, 65535)][int]$Port = 18767,
    [string]$PythonExecutable = '',
    [string]$Wheelhouse = '',
    [switch]$IncludeTests
)
. (Join-Path $PSScriptRoot 'windows-common.ps1')
Assert-AsterAdministrator
$origin = [Uri]$PublicOrigin
if (-not $origin.IsAbsoluteUri -or $origin.Scheme -ne 'https' -or -not $origin.Host -or
    $origin.UserInfo -or $origin.Query -or $origin.Fragment -or $origin.AbsolutePath -ne '/') {
    throw 'PublicOrigin must be a bare HTTPS origin, for example https://mcp.example.com.'
}
$PublicOrigin = $origin.GetLeftPart([UriPartial]::Authority)
$candidatePath = Join-Path $script:AsterSourceRoot 'candidate-manifest.json'
$candidateId = Assert-AsterCandidate $candidatePath
$state = Get-AsterStateDirectory
Assert-AsterNoListener $Port
$configPath = Join-Path $state 'gateway-config.json'
if (Test-Path -LiteralPath $configPath) {
    $previous = Get-Content -LiteralPath $configPath -Encoding UTF8 -Raw | ConvertFrom-Json
    Assert-AsterNoListener ([int]$previous.port)
}
$runtimePath = Join-Path $state 'gateway-runtime.json'
if (Test-Path -LiteralPath $runtimePath) {
    $previousRuntime = Get-Content -LiteralPath $runtimePath -Encoding UTF8 -Raw | ConvertFrom-Json
    if (Get-Process -Id ([int]$previousRuntime.pid) -ErrorAction SilentlyContinue) {
        throw 'A process recorded in the previous runtime manifest is still alive. Identify and stop it before setup.'
    }
}
$null = New-Item -ItemType Directory -Path $state -Force
Protect-AsterState $state
$workspace = Join-Path $state 'workspace'
if (Test-Path -LiteralPath $workspace) {
    $workspaceItem = Get-Item -LiteralPath $workspace
    if (-not $workspaceItem.PSIsContainer -or ($workspaceItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'The default workspace must be a real directory inside the protected state directory.'
    }
} else { $null = New-Item -ItemType Directory -Path $workspace }
$prefix = @()
if (-not $PythonExecutable) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $launcher) { throw 'Python launcher not found. Supply -PythonExecutable with an installed Python 3.11+ x64 interpreter.' }
    $PythonExecutable = $launcher.Source
    $prefix = @('-3')
}
Invoke-AsterPython $PythonExecutable ($prefix + @('-c', "import sys,struct; assert sys.version_info >= (3,11) and struct.calcsize('P')==8, 'Python 3.11+ x64 required'; print(sys.executable)"))
$venv = Join-Path $script:AsterSourceRoot '.venv'
$python = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    if (Test-Path -LiteralPath $venv) { throw 'Incomplete .venv exists. Review it before creating the Python environment.' }
    Invoke-AsterPython $PythonExecutable ($prefix + @('-m', 'venv', $venv))
}
$install = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input')
if ($Wheelhouse) {
    $Wheelhouse = (Resolve-Path -LiteralPath $Wheelhouse).ProviderPath
    $install += @('--no-index', '--find-links', $Wheelhouse)
}
$lockName = 'requirements-windows-lock.txt'
if ($IncludeTests) { $lockName = 'requirements-windows-test-lock.txt' }
$lockPath = Join-Path $script:AsterSourceRoot $lockName
if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) { throw "Tested dependency lock is missing: $lockName" }
$install += @('-r', $lockPath)
Invoke-AsterPython $python $install
Invoke-AsterPython $python @('-m', 'pip', 'check')
$dependencies = @(& $python -m pip freeze --all)
if ($LASTEXITCODE -ne 0) { throw 'Cannot record the Python dependency inventory.' }
[IO.File]::WriteAllText((Join-Path $state 'python-dependencies.txt'), (($dependencies -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))
$allowlist = Join-Path $state 'admin-clients.json'
if (-not (Test-Path -LiteralPath $allowlist)) { [IO.File]::WriteAllText($allowlist, "[]`n", (New-Object Text.UTF8Encoding($false))) }
Invoke-AsterPython $python @('-c', "import json,sys; value=json.load(open(sys.argv[1],encoding='utf-8-sig')); assert isinstance(value,list) and all(isinstance(item,str) for item in value), 'Client allowlist must be a JSON list of strings'", $allowlist)
$null = Assert-AsterCandidate $candidatePath $candidateId
$protectedManifest = Join-Path $state 'candidate-manifest.json'
Copy-Item -LiteralPath $candidatePath -Destination $protectedManifest -Force
$operator = Join-Path $state 'operator-windows.ps1'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'operator-windows.ps1') -Destination $operator -Force
$operatorHash = (Get-FileHash -LiteralPath $operator -Algorithm SHA256).Hash.ToLowerInvariant()
$config = [ordered]@{
    schema_version = 1; source_root = $script:AsterSourceRoot; python_executable = $python
    workspace_root = $workspace; operator_script = $operator; operator_sha256 = $operatorHash
    oauth_db = (Join-Path $state 'windows-oauth.sqlite'); admin_clients = $allowlist
    public_origin = $PublicOrigin; port = $Port; candidate_id = $candidateId
    source_manifest = $protectedManifest; runtime_manifest = $runtimePath
    configured_at = [DateTimeOffset]::UtcNow.ToString('o')
}
Write-AsterJson $configPath $config
Protect-AsterState $state
foreach ($controlFile in @($configPath, $protectedManifest, $operator, $allowlist)) {
    Protect-AsterControlFileOwner $controlFile
}
[pscustomobject]@{ status = 'configured'; config_path = $configPath; candidate_id = $candidateId; source_root = $script:AsterSourceRoot; python_executable = $python; workspace_root = $workspace; emergency_operator = $operator; dependency_lock = $lockName; dependency_inventory = (Join-Path $state 'python-dependencies.txt'); network_changes = $false; client_permissions_changed = $false } | ConvertTo-Json
