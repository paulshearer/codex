#requires -Version 5.1
[CmdletBinding(DefaultParameterSetName='Local')]
param(
    [Parameter(Mandatory,ParameterSetName='Local')][string]$IsoPath,
    [Parameter(Mandatory,ParameterSetName='Download')][uri]$IsoUrl,
    [Parameter(Mandatory,ParameterSetName='Local')][Parameter(Mandatory,ParameterSetName='Download')][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$IsoSha256,
    [Parameter(Mandatory,ParameterSetName='Local')][Parameter(Mandatory,ParameterSetName='Download')][Parameter(ParameterSetName='Resume')][string]$PackageDirectory,
    [string]$StateRoot = "$env:ProgramData\CodexPermissive",
    [string]$VMName = 'CodexPermissive-Sandbox',
    [ValidateRange(1024,65535)][int]$ControllerPort = 8789,
    [string]$UpstreamEndpoint = 'https://sparky1.taileef59.ts.net:8445/v1',
    [string]$PublicEgressAddress,
    [string]$RelayCertificatePath,
    [string]$ControllerHome = $(if ($env:PERMISSIVE_HOME) {$env:PERMISSIVE_HOME} else {Join-Path $env:LOCALAPPDATA 'CodexPermissive'}),
    [Management.Automation.PSCredential]$GuestCredential,
    [Parameter(Mandatory,ParameterSetName='Resume')][switch]$Resume
)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SandboxAdministrator
$package = if ($Resume) {$null} else {Get-SandboxPackage $PackageDirectory}
$StateRoot = [IO.Path]::GetFullPath($StateRoot)
Set-SandboxPrivateDirectory $StateRoot
$sandboxes = Join-Path $StateRoot 'Sandboxes'
Set-SandboxPrivateDirectory $sandboxes
$existing = @(Get-ChildItem -LiteralPath $sandboxes -Directory | ForEach-Object {
    $candidate = Join-Path $_.FullName 'deployment.json'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { Read-SandboxBinding $candidate }
} | Where-Object {$_.vmName -eq $VMName})
if ($existing.Count) {
    if (!$Resume -or $existing.Count -ne 1) { throw 'Sandbox name already has state. Use -Resume for its unfinished provisioning.' }
    $binding = $existing[0]
    $vm = Get-VM -Id ([Guid]$binding.vmId)
    $vm | Get-VMNetworkAdapter | Disconnect-VMNetworkAdapter
    if (!$binding.PSObject.Properties['guestIp']) {
        $networkId = if ($binding.PSObject.Properties['bindingId']) {$binding.bindingId} else {$binding.sandboxId}
        $network = New-SandboxNetwork $StateRoot $networkId $PublicEgressAddress $vm.Id
        foreach ($key in $network.Keys) { $binding | Add-Member -NotePropertyName $key -NotePropertyValue $network[$key] -Force }
        $binding.status = 'awaiting-relay'
        Write-SandboxJson (Join-Path $binding.stateDirectory 'deployment.json') $binding
    }
    if ($binding.PSObject.Properties['portableControllerSource'] -and !(Test-Path -LiteralPath (Join-Path $binding.controllerHome 'environment.json'))) {
        & (Join-Path $binding.packagePath 'python\python.exe') -m permissive.portability --import-state --home $binding.controllerHome --source $binding.portableControllerSource --vm-id $binding.vmId | Out-Null
        if ($LASTEXITCODE) { throw 'Portable controller snapshot did not complete; guest remains disconnected.' }
        & (Join-Path $binding.packagePath 'python\python.exe') -m permissive.auth --home $binding.controllerHome init-local | Out-Null
        if ($LASTEXITCODE) { throw 'Destination controller identity initialization failed; guest remains disconnected.' }
    }
    Set-SandboxPerimeter $binding
    if ($vm.State -eq 'Off') { $vm | Start-VM }
    if (!$RelayCertificatePath) { $RelayCertificatePath = Initialize-SandboxRelay $binding (Join-Path $binding.stateDirectory 'deployment.json') }
    & (Join-Path $PSScriptRoot 'Complete-Sandbox.ps1') -BindingPath (Join-Path $binding.stateDirectory 'deployment.json') -RelayCertificatePath $RelayCertificatePath
    Write-Output (Join-Path $binding.stateDirectory 'deployment.json')
    return
}
if ($Resume) { throw 'No existing sandbox binding matches this VM name and state root.' }
if (Get-VM -Name $VMName -ErrorAction SilentlyContinue) { throw 'A VM with that name already exists outside this sandbox binding.' }
if (!@((Get-VMHostSupportedVersion).Version | Where-Object {[string]$_ -eq '9.0'}).Count) { throw 'This Hyper-V host does not support portable configuration version 9.0.' }
if (!$GuestCredential) {
    $bytes = New-Object byte[] 30
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $secure = ConvertTo-SecureString ([Convert]::ToBase64String($bytes) + '!aA1') -AsPlainText -Force
    [Array]::Clear($bytes, 0, $bytes.Length)
    $GuestCredential = New-Object Management.Automation.PSCredential('SandboxAdmin', $secure)
}
if ($GuestCredential.UserName -ne 'SandboxAdmin') { throw 'The dedicated guest account must be named SandboxAdmin.' }
$sandboxId = [Guid]::NewGuid().ToString()
$state = Join-Path $sandboxes $sandboxId
Set-SandboxPrivateDirectory $state
$credentialFile = Join-Path $state 'guest-credential.xml'
$GuestCredential | Export-Clixml -LiteralPath $credentialFile
if ($PSCmdlet.ParameterSetName -eq 'Download') {
    if ($IsoUrl.Scheme -ne 'https' -or $IsoUrl.UserInfo -or !($IsoUrl.DnsSafeHost -eq 'microsoft.com' -or $IsoUrl.DnsSafeHost.EndsWith('.microsoft.com'))) { throw 'ISO downloads must use an official Microsoft HTTPS URL.' }
    $IsoPath = Join-Path $state 'Windows11.iso'
    Invoke-WebRequest -Uri $IsoUrl -UseBasicParsing -OutFile $IsoPath
}
$IsoPath = (Resolve-Path -LiteralPath $IsoPath).Path
if ((Get-FileHash -LiteralPath $IsoPath -Algorithm SHA256).Hash -ne $IsoSha256) { throw 'Windows ISO SHA-256 does not match the independently supplied official pin.' }
$diskPath = Join-Path $state 'Windows11.vhdx'
$isoMounted = $false; $vhdMounted = $false
try {
    if (!(Get-DiskImage -ImagePath $IsoPath).Attached) {
        Mount-DiskImage -ImagePath $IsoPath -StorageType ISO -Access ReadOnly | Out-Null
        $isoMounted = $true
    }
    $media = (Get-DiskImage -ImagePath $IsoPath | Get-Volume).DriveLetter + ':\'
    $signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $media 'setup.exe')
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') { throw 'Installation media setup.exe is not validly signed by Microsoft.' }
    $image = Join-Path $media 'sources\install.wim'
    if (!(Test-Path -LiteralPath $image)) { $image = Join-Path $media 'sources\install.esd' }
    $pro = @(Get-WindowsImage -ImagePath $image | ForEach-Object { Get-WindowsImage -ImagePath $image -Index $_.ImageIndex } | Where-Object {$_.EditionId -eq 'Professional' -and $_.ImageName -like '*Windows 11*'})
    if ($pro.Count -ne 1) { throw 'The supplied ISO must contain exactly one Windows 11 Pro image.' }
    New-VHD -Path $diskPath -Dynamic -SizeBytes 160GB | Out-Null
    $disk = Mount-VHD -Path $diskPath -Passthru | Get-Disk
    $vhdMounted = $true
    $disk | Initialize-Disk -PartitionStyle GPT
    $efi = $disk | New-Partition -Size 260MB -GptType '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' -AssignDriveLetter
    $efi | Format-Volume -FileSystem FAT32 -NewFileSystemLabel System -Confirm:$false | Out-Null
    $disk | New-Partition -Size 16MB -GptType '{e3c9e316-0b5c-4db8-817d-f92df00215ae}' | Out-Null
    $os = $disk | New-Partition -UseMaximumSize -AssignDriveLetter
    $os | Format-Volume -FileSystem NTFS -NewFileSystemLabel Windows -Confirm:$false | Out-Null
    $osRoot = $os.DriveLetter + ':\'
    & dism.exe /Apply-Image "/ImageFile:$image" "/Index:$($pro[0].ImageIndex)" "/ApplyDir:$osRoot" /CheckIntegrity | Out-Null
    if ($LASTEXITCODE) { throw 'Applying the official Windows image failed.' }
    & bcdboot.exe (Join-Path $osRoot 'Windows') /s ($efi.DriveLetter + ':') /f UEFI | Out-Null
    if ($LASTEXITCODE) { throw 'Creating the guest UEFI boot files failed.' }
    Copy-Item -LiteralPath $package -Destination (Join-Path $osRoot 'CodexPermissive') -Recurse
    New-Item -ItemType Directory -Path (Join-Path $osRoot 'ProgramData\CodexPermissive') -Force | Out-Null
    $panther = Join-Path $osRoot 'Windows\Panther'
    New-Item -ItemType Directory -Path $panther -Force | Out-Null
    $password = [Security.SecurityElement]::Escape($GuestCredential.GetNetworkCredential().Password)
    # A guest-only setup credential is transiently required by Windows unattend.
    # Its directory is host-ACL restricted; Initialize-Guest removes cached answers.
    $answer = @"
<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
 <settings pass="specialize"><component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><ComputerName>CP-$($sandboxId.Substring(0,8))</ComputerName><TimeZone>Eastern Standard Time</TimeZone></component></settings>
 <settings pass="oobeSystem"><component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale></component>
 <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS"><OOBE><HideEULAPage>true</HideEULAPage><HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE><ProtectYourPC>3</ProtectYourPC></OOBE><UserAccounts><LocalAccounts><LocalAccount wcm:action="add"><Name>SandboxAdmin</Name><Group>Administrators</Group><Password><Value>$password</Value><PlainText>true</PlainText></Password></LocalAccount></LocalAccounts></UserAccounts><AutoLogon><Username>SandboxAdmin</Username><Enabled>true</Enabled><LogonCount>1</LogonCount><Password><Value>$password</Value><PlainText>true</PlainText></Password></AutoLogon></component></settings>
</unattend>
"@
    [IO.File]::WriteAllText((Join-Path $panther 'Unattend.xml'), $answer, (New-Object Text.UTF8Encoding($false)))
    $answer = $null; $password = $null
} finally {
    if ($vhdMounted) { Dismount-VHD -Path $diskPath }
    if ($isoMounted) { Dismount-DiskImage -ImagePath $IsoPath | Out-Null }
}
$vm = New-VM -Name $VMName -Generation 2 -Version '9.0' -MemoryStartupBytes 16GB -VHDPath $diskPath -Path $state
Set-VM -VM $vm -ProcessorCount 8 -AutomaticStartAction Nothing -AutomaticStopAction ShutDown -AutomaticCheckpointsEnabled $false
Set-VMFirmware -VM $vm -EnableSecureBoot Off
Set-VMSecurity -VM $vm -EncryptStateAndVmMigrationTraffic $false
Disable-VMTPM -VM $vm
$vm | Get-VMNetworkAdapter | Disconnect-VMNetworkAdapter
$binding = [ordered]@{schema='codex-permissive-sandbox-binding/v1';role='controller';sandboxId=$sandboxId;vmId=$vm.Id.ToString();vmName=$VMName;packagePath=$package;stateDirectory=$state;controllerPort=$ControllerPort;controllerHome=[IO.Path]::GetFullPath($ControllerHome);guestPackagePath='C:\CodexPermissive';guestCredentialPath=$credentialFile;upstreamEndpoint=$UpstreamEndpoint;isoSha256=$IsoSha256.ToLowerInvariant();status='network-pending'}
$bindingPath = Join-Path $state 'deployment.json'
Write-SandboxJson $bindingPath $binding
$network = New-SandboxNetwork $StateRoot $sandboxId $PublicEgressAddress $vm.Id
foreach ($key in $network.Keys) { $binding[$key] = $network[$key] }
$binding.status = 'awaiting-relay'
Write-SandboxJson $bindingPath $binding
$binding = Read-SandboxBinding $bindingPath
Set-SandboxPerimeter $binding
$vm | Start-VM
if (!$RelayCertificatePath) { $RelayCertificatePath = Initialize-SandboxRelay $binding $bindingPath }
& (Join-Path $PSScriptRoot 'Complete-Sandbox.ps1') -BindingPath $bindingPath -RelayCertificatePath $RelayCertificatePath
Write-Output $bindingPath
