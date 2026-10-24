param([string]$MediaRoot = $PSScriptRoot)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run the disposable-VM foundation bootstrap as Administrator.'
}
$state = Join-Path $env:ProgramData 'AsterTestHarness'
New-Item -ItemType Directory -Path $state -Force | Out-Null
& icacls.exe $state '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not protect the test-harness directory.' }
Start-Transcript -Path (Join-Path $state 'foundation-transcript.txt') -Append | Out-Null
try {
    $manifest = Get-Content -LiteralPath (Join-Path $MediaRoot 'foundation-files.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $required = @('python-3.11.9-amd64.exe', 'qemu-ga-x86_64.msi', 'driver/vioser.cat', 'driver/vioser.inf', 'driver/vioser.sys')
    if (@($manifest.files).Count -ne $required.Count -or
        (Compare-Object $required @($manifest.files | ForEach-Object { [string]$_.name }))) {
        throw 'Foundation media must contain exactly the reviewed installer and serial-driver files.'
    }
    foreach ($entry in $manifest.files) {
        $path = Join-Path $MediaRoot ([string]$entry.name)
        $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -cne [string]$entry.sha256) { throw "Installer checksum mismatch: $($entry.name)" }
        if ($entry.name -in @('python-3.11.9-amd64.exe', 'driver/vioser.cat')) {
            $signature = Get-AuthenticodeSignature -FilePath $path
            if ($signature.Status -ne 'Valid') { throw "Required signature is not valid: $($entry.name): $($signature.Status)" }
            Write-Output ("VERIFIED_SIGNATURE " + $entry.name + ' SIGNER=' + $signature.SignerCertificate.Subject)
        }
        Write-Output ("VERIFIED_FILE " + $entry.name + ' SHA256=' + $actual)
    }
    # The QGA MSI's exact bytes were extracted from the official HTTPS vendor
    # bundle and checked against its payload hash; no wrapper executable is run.
    # Windows still verifies the signed driver catalog; test-signing is not enabled.
    if ([string]$manifest.vendor_source -notmatch '^https://fedorapeople\.org/groups/virt/virtio-win/direct-downloads/archive-virtio/' -or
        [string]$manifest.vendor_bundle_sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Vendor package provenance is missing.' }
    Write-Output ('VENDOR_PACKAGE_SOURCE ' + $manifest.vendor_source + ' BUNDLE_SHA256=' + $manifest.vendor_bundle_sha256)
    & pnputil.exe '/add-driver' (Join-Path $MediaRoot 'driver\vioser.inf') '/install'
    if ($LASTEXITCODE -notin @(0, 3010)) { throw "Serial driver installation failed: $LASTEXITCODE" }
    $guestInstaller = Join-Path $MediaRoot 'qemu-ga-x86_64.msi'
    $guestLog = Join-Path $state 'qemu-agent-install.log'
    $installed = Start-Process -FilePath 'msiexec.exe' -ArgumentList @('/i', ('"' + $guestInstaller + '"'), '/qn', '/norestart', '/L*v', ('"' + $guestLog + '"')) -Wait -PassThru
    if ($installed.ExitCode -notin @(0, 3010)) { throw "Guest agent installation failed: $($installed.ExitCode)" }
    Write-Output ('GUEST_AGENT_INSTALL_EXIT ' + $installed.ExitCode)
    $pythonTarget = Join-Path $env:ProgramFiles 'Python311'
    $python = Join-Path $pythonTarget 'python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        $pythonLog = Join-Path $state 'python-install.log'
        $installedPython = Start-Process -FilePath (Join-Path $MediaRoot 'python-3.11.9-amd64.exe') -ArgumentList @('/quiet', 'InstallAllUsers=1', 'Include_test=0', 'PrependPath=0', 'Include_launcher=0', ('TargetDir="' + $pythonTarget + '"'), '/log', ('"' + $pythonLog + '"')) -Wait -PassThru
        if ($installedPython.ExitCode -notin @(0, 3010)) { throw "Python installation failed: $($installedPython.ExitCode)" }
        Write-Output ('PYTHON_INSTALL_EXIT ' + $installedPython.ExitCode)
    }
    & $python --version
    if ($LASTEXITCODE -ne 0) { throw 'Installed Python could not run.' }
    $service = Get-Service -Name 'QEMU-GA' -ErrorAction Stop
    if ($service.Status -ne 'Running') { Start-Service -Name 'QEMU-GA' }
    $result = [ordered]@{
        completed_at = [DateTime]::UtcNow.ToString('o')
        computer_name = $env:COMPUTERNAME
        identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        python = $python
        guest_agent = (Get-Service -Name 'QEMU-GA').Status.ToString()
    }
    [IO.File]::WriteAllText((Join-Path $state 'foundation-result.json'), (($result | ConvertTo-Json) + "`n"), (New-Object Text.UTF8Encoding($false)))
    Write-Output 'ASTER_TEST_FOUNDATION_COMPLETE'
} finally {
    Stop-Transcript | Out-Null
}
