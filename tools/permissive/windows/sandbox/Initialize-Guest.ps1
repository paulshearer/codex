# Runs INSIDE the sandbox, over PowerShell Direct. Never run this on the host.
#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$GuestIp,
    [Parameter(Mandatory)][string]$GatewayIp,
    [Parameter(Mandatory)][string]$MacAddress,
    [Parameter(Mandatory)][string]$DeploymentJson,
    [Parameter(Mandatory)][Management.Automation.PSCredential]$GuestCredential,
    [string]$RelayCertificatePath
)
$ErrorActionPreference = 'Stop'
if ((Get-CimInstance Win32_ComputerSystem).Model -ne 'Virtual Machine' -or !(Test-Path -LiteralPath 'C:\CodexPermissive\python\python.exe')) { throw 'Guest initialization requires the provisioned Hyper-V guest image.' }
$config = $DeploymentJson | ConvertFrom-Json
if ($config.role -ne 'guest' -or $GuestCredential.UserName -ne 'SandboxAdmin') { throw 'Invalid guest deployment/account.' }
foreach ($ip in @($GuestIp, $GatewayIp)) { if ([Net.IPAddress]::Parse($ip).AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'IPv4 network binding required.' } }
$adapter = @(Get-NetAdapter | Where-Object {($_.MacAddress -replace '[-:]','') -eq ($MacAddress -replace '[-:]','')})
if ($adapter.Count -ne 1) { throw 'Guest adapter cannot be identified by its Hyper-V MAC.' }
Set-NetIPInterface -InterfaceIndex $adapter[0].ifIndex -AddressFamily IPv4 -Dhcp Disabled
Get-NetIPAddress -InterfaceIndex $adapter[0].ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:$false
Get-NetRoute -InterfaceIndex $adapter[0].ifIndex -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | Remove-NetRoute -Confirm:$false
New-NetIPAddress -InterfaceIndex $adapter[0].ifIndex -IPAddress $GuestIp -PrefixLength 30 -DefaultGateway $GatewayIp | Out-Null
Set-DnsClientServerAddress -InterfaceIndex $adapter[0].ifIndex -ServerAddresses '1.1.1.1','9.9.9.9'
$hostsPath = Join-Path $env:WINDIR 'System32\drivers\etc\hosts'
$hosts = @(Get-Content -LiteralPath $hostsPath | Where-Object {$_ -notmatch '(?i)(^|\s)permissive-gateway\.internal(\s|$)'})
[IO.File]::WriteAllLines($hostsPath, ($hosts + "$GatewayIp permissive-gateway.internal"), (New-Object Text.UTF8Encoding($false)))
$state = 'C:\ProgramData\CodexPermissive'
New-Item -ItemType Directory -Path $state -Force | Out-Null
[IO.File]::WriteAllText('C:\CodexPermissive\deployment.json', $DeploymentJson, (New-Object Text.UTF8Encoding($false)))
foreach ($entry in @{PERMISSIVE_PACKAGE='C:\CodexPermissive';PERMISSIVE_HOME='C:\ProgramData\CodexPermissiveGuest';PERMISSIVE_DEPLOYMENT='C:\CodexPermissive\deployment.json';PERMISSIVE_ROLE='guest'}.GetEnumerator()) {
    [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Machine')
}
$nativeHostPath = 'C:\CodexPermissive\browser\native-host.json'
$nativeHost = @{name='com.codex_permissive.browser';description='Codex Permissive guest browser connection';type='stdio';path='C:\CodexPermissive\native-host.exe';allowed_origins=@('chrome-extension://mnldpjghemdjnoefihnifnpfhlaihamk/')} | ConvertTo-Json
[IO.File]::WriteAllText($nativeHostPath, $nativeHost, (New-Object Text.UTF8Encoding($false)))
foreach ($vendor in @('Google\Chrome','Microsoft\Edge')) {
    $key = 'HKLM:\SOFTWARE\' + $vendor + '\NativeMessagingHosts\com.codex_permissive.browser'
    New-Item -Path $key -Force | Out-Null
    Set-Item -Path $key -Value $nativeHostPath
}
if ($RelayCertificatePath) {
    $pem = Get-Content -LiteralPath $RelayCertificatePath -Raw
    $match = [regex]::Match($pem, '-----BEGIN CERTIFICATE-----\s*([A-Za-z0-9+/=\r\n]+)\s*-----END CERTIFICATE-----')
    if (!$match.Success) { throw 'Relay trust file must contain a PEM certificate.' }
    $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2(,[Convert]::FromBase64String(($match.Groups[1].Value -replace '\s','')))
    $oldTrust = Join-Path $state 'relay-trust-thumbprint.txt'
    if (Test-Path -LiteralPath $oldTrust) {
        $old = (Get-Content -LiteralPath $oldTrust -Raw).Trim()
        if ($old -match '^[A-Fa-f0-9]{40}$' -and $old -ne $cert.Thumbprint) { Remove-Item -LiteralPath ("Cert:\LocalMachine\Root\$old") -ErrorAction SilentlyContinue }
    }
    $store = New-Object Security.Cryptography.X509Certificates.X509Store('Root', 'LocalMachine')
    try { $store.Open('ReadWrite'); $store.Add($cert) } finally { $store.Close() }
    [IO.File]::WriteAllText($oldTrust, $cert.Thumbprint)
}
if (!('PermissiveGuestLsa' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class PermissiveGuestLsa {
 [StructLayout(LayoutKind.Sequential)] struct U { public ushort Length, MaximumLength; public IntPtr Buffer; }
 [StructLayout(LayoutKind.Sequential)] struct O { public int Length; public IntPtr RootDirectory, ObjectName; public uint Attributes; public IntPtr SecurityDescriptor, SecurityQualityOfService; }
 [DllImport("advapi32.dll")] static extern uint LsaOpenPolicy(IntPtr system, ref O attributes, uint access, out IntPtr handle);
 [DllImport("advapi32.dll")] static extern uint LsaStorePrivateData(IntPtr handle, ref U key, ref U data);
 [DllImport("advapi32.dll")] static extern uint LsaClose(IntPtr handle);
 [DllImport("advapi32.dll")] static extern uint LsaNtStatusToWinError(uint status);
 public static void SetPassword(string password) {
  var key = new U(); var data = new U(); IntPtr policy = IntPtr.Zero;
  try {
   key.Buffer = Marshal.StringToCoTaskMemUni("DefaultPassword"); key.Length = 30; key.MaximumLength = 32;
   data.Buffer = Marshal.StringToCoTaskMemUni(password); data.Length = checked((ushort)(password.Length * 2)); data.MaximumLength = checked((ushort)(data.Length + 2));
   var attributes = new O(); attributes.Length = Marshal.SizeOf(typeof(O));
   uint result = LsaOpenPolicy(IntPtr.Zero, ref attributes, 0x24, out policy);
   if (result != 0) throw new Win32Exception((int)LsaNtStatusToWinError(result));
   result = LsaStorePrivateData(policy, ref key, ref data);
   if (result != 0) throw new Win32Exception((int)LsaNtStatusToWinError(result));
  } finally {
   if (policy != IntPtr.Zero) LsaClose(policy);
   if (data.Buffer != IntPtr.Zero) Marshal.ZeroFreeCoTaskMemUnicode(data.Buffer);
   if (key.Buffer != IntPtr.Zero) Marshal.FreeCoTaskMem(key.Buffer);
  }
 }
}
'@
}
Set-LocalUser -Name SandboxAdmin -Password $GuestCredential.Password -PasswordNeverExpires $true
[PermissiveGuestLsa]::SetPassword($GuestCredential.GetNetworkCredential().Password)
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty -Path $winlogon -Name DefaultUserName -Value SandboxAdmin
Set-ItemProperty -Path $winlogon -Name DefaultDomainName -Value $env:COMPUTERNAME
Set-ItemProperty -Path $winlogon -Name AutoAdminLogon -Value '1'
Remove-ItemProperty -Path $winlogon -Name DefaultPassword,AutoLogonCount -ErrorAction SilentlyContinue
New-Item -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\BitLocker' -Force | Out-Null
Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\BitLocker' -Name PreventDeviceEncryption -Value 1 -Type DWord
& powercfg.exe /hibernate off | Out-Null
& powercfg.exe /change standby-timeout-ac 0 | Out-Null
& powercfg.exe /change monitor-timeout-ac 0 | Out-Null
# Configure the dedicated interactive account, even when this invocation runs
# in a separate PowerShell Direct session. All of these remain guest-owned.
$adminSid = (Get-LocalUser -Name SandboxAdmin).SID.Value
$desktopKey = 'Registry::HKEY_USERS\' + $adminSid + '\Control Panel\Desktop'
if (Test-Path -LiteralPath $desktopKey) {
    Set-ItemProperty -LiteralPath $desktopKey -Name ScreenSaveActive -Value '0'
    Set-ItemProperty -LiteralPath $desktopKey -Name ScreenSaverIsSecure -Value '0'
}
$systemPolicy = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System'
Set-ItemProperty -Path $systemPolicy -Name InactivityTimeoutSecs -Value 0 -Type DWord
$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\CodexPermissive\windows\sandbox\Start-GuestRunner.ps1"'
$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\SandboxAdmin" -LogonType Interactive -RunLevel Highest
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:COMPUTERNAME\SandboxAdmin"
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName 'CodexPermissiveGuest' -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Force | Out-Null
foreach ($answer in @('C:\Windows\Panther\Unattend.xml', 'C:\Windows\Panther\Unattend\Unattend.xml', 'C:\Windows\System32\Sysprep\Unattend.xml')) {
    if (Test-Path -LiteralPath $answer -PathType Leaf) { Remove-Item -LiteralPath $answer -Force }
}
if ($RelayCertificatePath) { Start-ScheduledTask -TaskName 'CodexPermissiveGuest' }
