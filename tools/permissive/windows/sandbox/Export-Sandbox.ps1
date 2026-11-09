#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)][string]$BindingPath,
      [Parameter(Mandatory)][Alias('OutputDirectory')][string]$OutputPath,
      [Security.SecureString]$TransportPassword,
      [switch]$TransportPasswordStdin)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SandboxAdministrator
$binding = Read-SandboxBinding $BindingPath
$password = Get-SandboxTransportPassword $TransportPassword -TransportPasswordStdin:$TransportPasswordStdin
$output = [IO.Path]::GetFullPath($OutputPath)
if (Test-Path -LiteralPath $output) { throw 'Portable bundle output must be a new directory.' }
$home = [IO.Path]::GetFullPath($binding.controllerHome)
if ($output.StartsWith($home.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Export output must be outside controller home.' }
Set-SandboxPrivateDirectory $output
$environmentPath = Join-Path $home 'environment.json'
if (Test-Path -LiteralPath $environmentPath) { $environment = Get-Content -LiteralPath $environmentPath -Raw | ConvertFrom-Json } else {
    New-Item -ItemType Directory -Path $home -Force | Out-Null
    $environment = [pscustomobject]@{environmentId=$binding.sandboxId;vmId=$binding.vmId;generation=1;phase='ready'}
}
# Fence submissions before touching VM state; never auto-unfence a failed export.
if ($environment.phase -ne 'exported' -and (Test-Path -LiteralPath (Join-Path $home 'service.lock'))) {
    $lock = $null
    try {
        $lock = [IO.File]::Open((Join-Path $home 'service.lock'), 'Open', 'ReadWrite', 'ReadWrite')
        $lock.Lock(0,1)
        $lock.Unlock(0,1)
    } catch { throw 'The controller is active. Prepare export through its signed /v1/environment/export API before running this helper.' }
    finally { if ($lock) {$lock.Dispose()} }
}
$environment.phase = 'exported'
Write-SandboxJson $environmentPath $environment
# Stop phone admission and invalidate outstanding enrollment before snapshotting.
& (Join-Path $binding.packagePath 'windows/mobile-companion.ps1') -Action Fence -PackageDirectory $binding.packagePath -HomeDirectory $home | Out-Null
if (!$?) { throw 'Could not fence Android companion access; source remains fenced.' }
$vm = Get-VM -Id ([Guid]$binding.vmId)
$adapter = @($vm | Get-VMNetworkAdapter)
if ($adapter.Count -ne 1) { throw 'Portable export requires exactly one guest adapter.' }
$priorSwitch = $adapter[0].SwitchName
try {
    $adapter[0] | Disconnect-VMNetworkAdapter
    # A paused guest must not regain network access while it shuts down.
    if ($vm.State -eq 'Paused') { $vm | Resume-VM }
    if ((Get-VM -Id $vm.Id).State -ne 'Off') { $vm | Stop-VM }
    if ((Get-VM -Id $vm.Id).State -ne 'Off') { throw 'Guest did not shut down; no running or saved state export was taken.' }
    Set-VM -VM $vm -AutomaticStartAction Nothing
    $vm | Export-VM -Path (Join-Path $output 'vm')
} finally {
    if ($priorSwitch -and (Get-VM -Id $vm.Id).State -eq 'Off') { $vm | Get-VMNetworkAdapter | Connect-VMNetworkAdapter -SwitchName $priorSwitch }
}
$guestCredential = Get-SandboxCredential $binding
$credentialJson = @{username=$guestCredential.UserName;password=$guestCredential.GetNetworkCredential().Password} | ConvertTo-Json -Compress
$envelope = Invoke-SandboxSecretCodec $binding.packagePath seal $credentialJson $password
$credentialJson = $null
Write-SandboxJson (Join-Path $output 'guest-credential.envelope.json') $envelope
& (Join-Path $binding.packagePath 'python\python.exe') -m permissive.portability --export-state --home $home --output (Join-Path $output 'controller') | Out-Null
if ($LASTEXITCODE) { throw 'Portable controller snapshot failed. Source remains fenced.' }
Copy-Item -LiteralPath $PSScriptRoot -Destination (Join-Path $output 'toolkit') -Recurse
$files = @{}
Get-ChildItem -LiteralPath $output -File -Recurse | ForEach-Object {
    if ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Portable export cannot contain reparse points.' }
    $relative = $_.FullName.Substring($output.Length + 1).Replace('\','/')
    $files[$relative] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
$config = @(Get-ChildItem -LiteralPath (Join-Path $output 'vm') -Filter '*.vmcx' -Recurse -File | Where-Object {$_.Directory.Name -eq 'Virtual Machines'})
if ($config.Count -ne 1) { throw 'Expected one portable root VM configuration.' }
$manifest = @{schema='codex-permissive-sandbox-bundle/v1';sandboxId=$binding.sandboxId;configurationVersion='9.0';architecture='x64';generation=2;vmConfiguration=$config[0].FullName.Substring($output.Length+1).Replace('\','/');files=$files;credentialEnvelope='guest-credential.envelope.json';controllerSnapshot='controller';networkProfile='public-internet-and-inference';secureBootRequired=$false;tpmRequired=$false;bitLockerRequired=$false}
Write-SandboxJson (Join-Path $output 'sandbox-manifest.json') $manifest
$binding.status = 'exported'
Write-SandboxJson $BindingPath $binding
Install-SandboxControllerBinding $BindingPath $binding.packagePath
Write-Output $output
