#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)][string]$BundlePath,
      [Parameter(Mandatory)][string]$PackageDirectory,
      [Alias('StateRoot')][string]$RootPath = "$env:ProgramData\CodexPermissive",
      [string]$ControllerHome,
      [string]$VMName = 'CodexPermissive-Sandbox',
      [ValidateRange(1024,65535)][int]$ControllerPort = 8789,
      [string]$UpstreamEndpoint = 'https://sparky1.taileef59.ts.net:8445/v1',
      [string]$PublicEgressAddress,
      [Security.SecureString]$TransportPassword,
      [switch]$TransportPasswordStdin)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SandboxAdministrator
$package = Get-SandboxPackage $PackageDirectory
$bundle = (Resolve-Path -LiteralPath $BundlePath).Path
$manifest = Get-Content -LiteralPath (Join-Path $bundle 'sandbox-manifest.json') -Raw | ConvertFrom-Json
if ($manifest.schema -ne 'codex-permissive-sandbox-bundle/v1' -or $manifest.architecture -ne 'x64' -or $manifest.generation -ne 2) { throw 'Unsupported portable sandbox bundle.' }
if (!@((Get-VMHostSupportedVersion).Version | Where-Object {[string]$_ -eq $manifest.configurationVersion}).Count) { throw 'Destination does not support this VM configuration version.' }
$nats = @(Get-NetNat)
if (@($nats | Where-Object {$_.Name -ne 'CodexPermissive-NAT'}).Count) { throw 'An unrelated WinNAT exists; import did not modify it.' }
foreach ($file in $manifest.files.PSObject.Properties) {
    $path = Get-SandboxBundleFile $bundle $file.Name
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $file.Value) { throw 'Portable file SHA-256 mismatch.' }
}
foreach ($required in @($manifest.vmConfiguration,$manifest.credentialEnvelope,($manifest.controllerSnapshot + '/controller-manifest.json'))) {
    if (@($manifest.files.PSObject.Properties.Name) -notcontains $required) { throw 'Portable metadata references an unhashed required file.' }
}
if (Get-VM -Name $VMName -ErrorAction SilentlyContinue) { throw 'Destination VM name is already in use.' }
$password = Get-SandboxTransportPassword $TransportPassword -TransportPasswordStdin:$TransportPasswordStdin
$envelope = Get-Content -LiteralPath (Get-SandboxBundleFile $bundle $manifest.credentialEnvelope) -Raw | ConvertFrom-Json
$secret = Invoke-SandboxSecretCodec $package open $envelope $password
$credentialData = $secret | ConvertFrom-Json
if ($credentialData.username -ne 'SandboxAdmin') { throw 'Portable credential must identify the dedicated guest administrator.' }
$credential = New-Object Management.Automation.PSCredential('SandboxAdmin', (ConvertTo-SecureString $credentialData.password -AsPlainText -Force))
$secret = $null; $credentialData = $null
$sandboxId = [Guid]::Parse($manifest.sandboxId).ToString()
$state = Join-Path ([IO.Path]::GetFullPath($RootPath)) ('Sandboxes\' + [Guid]::NewGuid().ToString())
Set-SandboxPrivateDirectory $state
if (!$ControllerHome) { $ControllerHome = Join-Path $state 'controller' }
if ((Test-Path -LiteralPath $ControllerHome) -and @(Get-ChildItem -LiteralPath $ControllerHome -Force).Count) { throw 'Destination controller home must be empty.' }
$config = Get-SandboxBundleFile $bundle $manifest.vmConfiguration
$sourceDisks = Join-Path (Split-Path (Split-Path $config -Parent) -Parent) 'Virtual Hard Disks'
if (!(Test-Path -LiteralPath $sourceDisks -PathType Container)) { throw 'Portable virtual disks are missing.' }
$report = Compare-VM -Path $config -Copy -GenerateNewId -VirtualMachinePath $state -VhdSourcePath $sourceDisks -VhdDestinationPath (Join-Path $state 'Disks') -SnapshotFilePath (Join-Path $state 'Snapshots') -SmartPagingFilePath (Join-Path $state 'Paging')
foreach ($problem in @($report.Incompatibilities)) {
    if ($problem.Source -is [Microsoft.HyperV.PowerShell.VMNetworkAdapter]) { $problem.Source | Disconnect-VMNetworkAdapter } else { throw 'Destination VM compatibility check failed. No guest was started.' }
}
$report = Compare-VM -CompatibilityReport $report
if (@($report.Incompatibilities).Count) { throw 'Unresolved destination VM compatibility issue.' }
$vm = Import-VM -CompatibilityReport $report
Rename-VM -VM $vm -NewName $VMName
$vm | Get-VMNetworkAdapter | Disconnect-VMNetworkAdapter
Set-VM -VM $vm -AutomaticStartAction Nothing -AutomaticCheckpointsEnabled $false
Set-VMFirmware -VM $vm -EnableSecureBoot Off
Set-VMSecurity -VM $vm -EncryptStateAndVmMigrationTraffic $false
Disable-VMTPM -VM $vm
$credentialPath = Join-Path $state 'guest-credential.xml'
$credential | Export-Clixml -LiteralPath $credentialPath
# A new binding ID isolates clones imported into the same destination host.
$bindingId = [Guid]::NewGuid().ToString()
$binding = [ordered]@{schema='codex-permissive-sandbox-binding/v1';role='controller';sandboxId=$sandboxId;bindingId=$bindingId;vmId=$vm.Id.ToString();vmName=$VMName;packagePath=$package;stateDirectory=$state;controllerPort=$ControllerPort;controllerHome=[IO.Path]::GetFullPath($ControllerHome);guestPackagePath='C:\CodexPermissive';guestCredentialPath=$credentialPath;upstreamEndpoint=$UpstreamEndpoint;portableControllerSource=(Join-Path $bundle $manifest.controllerSnapshot);status='network-pending'}
$bindingPath = Join-Path $state 'deployment.json'
Write-SandboxJson $bindingPath $binding
$network = New-SandboxNetwork ([IO.Path]::GetFullPath($RootPath)) $bindingId $PublicEgressAddress $vm.Id
foreach ($key in $network.Keys) { $binding[$key] = $network[$key] }
$binding.status = 'awaiting-relay'
Write-SandboxJson $bindingPath $binding
$binding = Read-SandboxBinding $bindingPath
Set-SandboxPerimeter $binding
& (Join-Path $package 'python\python.exe') -m permissive.portability --import-state --home $ControllerHome --source (Join-Path $bundle $manifest.controllerSnapshot) --vm-id $binding.vmId | Out-Null
if ($LASTEXITCODE) { throw 'Controller state import failed; VM remains off and disconnected.' }
# The portable registry deliberately excludes source-local client identities.
# Explicit destination setup generates and authorizes its own DPAPI clients.
& (Join-Path $package 'python\python.exe') -m permissive.auth --home $ControllerHome init-local | Out-Null
if ($LASTEXITCODE) { throw 'Destination controller identity initialization failed; guest remains disconnected.' }
$certificate = Initialize-SandboxRelay $binding $bindingPath
$vm | Start-VM
& (Join-Path $PSScriptRoot 'Complete-Sandbox.ps1') -BindingPath $bindingPath -RelayCertificatePath $certificate
Write-Output $bindingPath
