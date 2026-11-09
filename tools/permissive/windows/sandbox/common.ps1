# Shared host-side primitives. Dot-sourcing this file does not change the machine.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-SandboxAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (!(New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this helper from an elevated Windows PowerShell 5.1 session on the Hyper-V host.'
    }
    if ($PSVersionTable.PSEdition -ne 'Desktop') { throw 'Use Windows PowerShell 5.1 (powershell.exe), which supplies PowerShell Direct.' }
    Import-Module Hyper-V -ErrorAction Stop
    Get-VMHost -ErrorAction Stop | Out-Null
}

function Set-SandboxPrivateDirectory([string]$Path) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544', [Security.Principal.WindowsIdentity]::GetCurrent().User.Value)) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($sid)), 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Write-SandboxJson([string]$Path, $Value) {
    $json = $Value | ConvertTo-Json -Depth 16
    [IO.File]::WriteAllText($Path, $json, (New-Object Text.UTF8Encoding($false)))
}

function Read-SandboxBinding([string]$BindingPath) {
    $binding = Get-Content -LiteralPath $BindingPath -Raw | ConvertFrom-Json
    if ($binding.schema -ne 'codex-permissive-sandbox-binding/v1' -or $binding.role -ne 'controller') { throw 'Unsupported sandbox host binding.' }
    [Guid]::Parse($binding.vmId) | Out-Null
    [Guid]::Parse($binding.sandboxId) | Out-Null
    return $binding
}

function Get-SandboxCredential($Binding) {
    $credential = Import-Clixml -LiteralPath $Binding.guestCredentialPath
    if ($credential -isnot [Management.Automation.PSCredential]) { throw 'Guest credential envelope is invalid or belongs to another Windows account/host.' }
    return $credential
}

function Get-SandboxPackage([string]$PackageDirectory) {
    $resolved = (Resolve-Path -LiteralPath $PackageDirectory).Path
    foreach ($relative in @('python/python.exe', 'lib/permissive/__main__.py', 'native/desktop-agent.exe', 'windows/sandbox/Initialize-Guest.ps1')) {
        if (!(Test-Path -LiteralPath (Join-Path $resolved $relative) -PathType Leaf)) { throw "Sandbox package is incomplete: $relative" }
    }
    return $resolved
}

function ConvertTo-SandboxIPv4Number([string]$Address) {
    $ip = [Net.IPAddress]::Parse($Address)
    if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) { throw 'Expected an IPv4 address.' }
    $bytes = $ip.GetAddressBytes()
    return [uint64]$bytes[0] * 16777216 + [uint64]$bytes[1] * 65536 + [uint64]$bytes[2] * 256 + $bytes[3]
}

function Test-SandboxPrefixOverlap([string]$Left, [string]$Right) {
    $a = $Left.Split('/'); $b = $Right.Split('/')
    $aBits = if ($a.Length -eq 2) { [int]$a[1] } else { 32 }
    $bBits = if ($b.Length -eq 2) { [int]$b[1] } else { 32 }
    if ($aBits -lt 0 -or $aBits -gt 32 -or $bBits -lt 0 -or $bBits -gt 32) { throw 'Invalid IPv4 prefix length.' }
    $size = [math]::Pow(2, 32 - [math]::Min($aBits, $bBits))
    return [math]::Floor((ConvertTo-SandboxIPv4Number $a[0]) / $size) -eq [math]::Floor((ConvertTo-SandboxIPv4Number $b[0]) / $size)
}

function Get-SandboxBundleFile([string]$Bundle, [string]$RelativePath) {
    if ([IO.Path]::IsPathRooted($RelativePath) -or $RelativePath.Contains(':')) { throw 'Portable paths must be relative regular files.' }
    $root = [IO.Path]::GetFullPath($Bundle).TrimEnd('\')
    $path = [IO.Path]::GetFullPath((Join-Path $root $RelativePath))
    if (!$path.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Portable file escapes the bundle.' }
    $cursor = $root
    foreach ($part in $path.Substring($root.Length+1).Split('\')) {
        $cursor = Join-Path $cursor $part
        $item = Get-Item -LiteralPath $cursor
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Portable paths cannot traverse reparse points.' }
    }
    if ((Get-Item -LiteralPath $path).PSIsContainer) { throw 'Portable file must be a regular file.' }
    return $path
}

function Get-SandboxDenyPrefixes {
    # Non-global IPv4, including CGNAT/Tailscale, multicast, metadata and transition space.
    return @('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24', '192.88.99.0/24', '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4')
}

function Get-SandboxAclSpecification($Binding) {
    $rules = New-Object Collections.Generic.List[object]
    foreach ($direction in @('Inbound', 'Outbound')) {
        $rules.Add(@{Action='Deny'; Direction=$direction; RemoteIPAddress='ANY'; Weight=1})
        $rules.Add(@{Action='Deny'; Direction=$direction; RemoteIPAddress='::/0'; Weight=6000})
    }
    $weight = 1000
    foreach ($prefix in @($Binding.denyPrefixes | Sort-Object -Unique)) {
        $rules.Add(@{Action='Deny'; Direction='Outbound'; RemoteIPAddress=[string]$prefix; Weight=$weight})
        $weight++
    }
    foreach ($protocol in @('TCP', 'UDP')) {
        $rules.Add(@{Action='Allow'; Direction='Outbound'; LocalIPAddress=($Binding.guestIp + '/32'); RemoteIPAddress='0.0.0.0/0'; Protocol=$protocol; Stateful=$true; IdleSessionTimeout=3600; Weight=$(if ($protocol -eq 'TCP') {100} else {101})})
    }
    $rules.Add(@{Action='Allow'; Direction='Outbound'; LocalIPAddress=($Binding.guestIp + '/32'); RemoteIPAddress=($Binding.gatewayIp + '/32'); RemotePort='18445'; Protocol='TCP'; Stateful=$true; IdleSessionTimeout=3600; Weight=5000})
    return $rules.ToArray()
}

function Set-SandboxPerimeter($Binding) {
    $adapter = @(Get-VM -Id ([Guid]$Binding.vmId) | Get-VMNetworkAdapter)
    if ($adapter.Count -ne 1) { throw 'Sandbox must have exactly one virtual network adapter.' }
    if ($adapter[0].SwitchName) { throw 'Disconnect the sandbox adapter before rebuilding its perimeter.' }
    Set-VMNetworkAdapter -VMNetworkAdapter $adapter[0] -MacAddressSpoofing Off -DhcpGuard On -RouterGuard On -AllowTeaming Off -PortMirroring None
    Get-VMNetworkAdapterExtendedAcl -VMNetworkAdapter $adapter[0] | Remove-VMNetworkAdapterExtendedAcl
    foreach ($rule in @(Get-SandboxAclSpecification $Binding)) { Add-VMNetworkAdapterExtendedAcl -VMNetworkAdapter $adapter[0] @rule }
}

function Update-SandboxNetworkBinding($Binding) {
    $deny = @(Get-SandboxDenyPrefixes) + @($Binding.publicEgressAddress + '/32')
    $deny += @(Get-NetRoute -AddressFamily IPv4 | Where-Object {$_.DestinationPrefix -ne '0.0.0.0/0'} | Select-Object -ExpandProperty DestinationPrefix)
    $deny += @(Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object {$_.IPAddress + '/32'})
    $Binding.denyPrefixes = @($deny | Sort-Object -Unique)
}

function Test-SandboxPerimeter($Binding) {
    $adapters = @(Get-VM -Id ([Guid]$Binding.vmId) | Get-VMNetworkAdapter)
    if ($adapters.Count -ne 1) { throw 'Perimeter verification failed: unexpected network adapter count.' }
    $adapter = $adapters[0]
    if ([string]$adapter.MacAddressSpoofing -ne 'Off' -or [string]$adapter.DhcpGuard -ne 'On' -or [string]$adapter.RouterGuard -ne 'On') { throw 'Perimeter verification failed: virtual adapter guards changed.' }
    if ($adapter.SwitchName -and $adapter.SwitchName -ne $Binding.switchName) { throw 'Perimeter verification failed: adapter is connected to another switch.' }
    $switch = Get-VMSwitch -Name $Binding.switchName
    if ([string]$switch.SwitchType -ne 'Internal') { throw 'Perimeter verification failed: expected a dedicated internal switch.' }
    $peers = @(Get-VMNetworkAdapter -All | Where-Object { !$_.IsManagementOs -and $_.SwitchId -eq $switch.Id })
    if (@($peers | Where-Object {$_.VMId -ne [Guid]$Binding.vmId}).Count) { throw 'Perimeter verification failed: another VM shares the switch.' }
    $actual = @(Get-VMNetworkAdapterExtendedAcl -VMNetworkAdapter $adapter)
    $expected = @(Get-SandboxAclSpecification $Binding)
    if ($actual.Count -ne $expected.Count) { throw 'Perimeter verification failed: ACL count changed.' }
    foreach ($rule in $expected) {
        $found = @($actual | Where-Object {[int]$_.Weight -eq $rule.Weight -and [string]$_.Direction -eq $rule.Direction})
        if ($found.Count -ne 1) { throw 'Perimeter verification failed: missing or duplicate ACL weight.' }
        foreach ($property in $rule.Keys) {
            $actualValue = [string]$found[0].$property
            $expectedValue = [string]$rule[$property]
            if ($property -like '*IPAddress' -and $actualValue -ne 'ANY' -and $actualValue -ne '*' -and !$actualValue.Contains('/')) { $actualValue += '/32' }
            if ($actualValue -eq '*') { $actualValue = 'ANY' }
            if ($property -eq 'Protocol') { if ($actualValue -eq '6') {$actualValue='TCP'}; if ($actualValue -eq '17') {$actualValue='UDP'} }
            if ($actualValue -ne $expectedValue) { throw "Perimeter verification failed: ACL $property changed." }
        }
    }
    $nat = Get-NetNat -Name $Binding.natName
    if ($nat.InternalIPInterfaceAddressPrefix -ne $Binding.natPrefix) { throw 'Perimeter verification failed: NAT prefix changed.' }
    if (@(Get-NetNatStaticMapping -NatName $Binding.natName).Count) { throw 'Perimeter verification failed: incoming NAT mappings are present.' }
    return $true
}

function New-SandboxNetwork([string]$StateRoot, [string]$SandboxId, [string]$PublicEgressAddress, [Guid]$OwnerVMId) {
    if (!$PublicEgressAddress) { $PublicEgressAddress = ([string](Invoke-RestMethod -Uri 'https://api.ipify.org' -TimeoutSec 20)).Trim() }
    ConvertTo-SandboxIPv4Number $PublicEgressAddress | Out-Null
    if (@((Get-SandboxDenyPrefixes) | Where-Object {Test-SandboxPrefixOverlap $_ ($PublicEgressAddress + '/32')}).Count) { throw 'Public egress address discovery returned a non-global address.' }
    $networkRoot = Join-Path $StateRoot 'Network'
    Set-SandboxPrivateDirectory $networkRoot
    $networkFile = Join-Path $networkRoot 'binding.json'
    $nats = @(Get-NetNat)
    if (@($nats | Where-Object {$_.Name -ne 'CodexPermissive-NAT'}).Count) { throw 'An unrelated WinNAT exists. No existing NAT was changed. Use a host with a compatible prepared sandbox network.' }
    if ($nats.Count) {
        if (!(Test-Path -LiteralPath $networkFile)) { throw 'Existing NAT ownership cannot be verified.' }
        $network = Get-Content -LiteralPath $networkFile -Raw | ConvertFrom-Json
        if ($network.schema -ne 'codex-permissive-network/v1' -or $nats[0].InternalIPInterfaceAddressPrefix -ne $network.prefix) { throw 'Existing sandbox NAT does not match its host binding.' }
    } else {
        $routes = @(Get-NetRoute -AddressFamily IPv4 | Where-Object {$_.DestinationPrefix -ne '0.0.0.0/0'} | Select-Object -ExpandProperty DestinationPrefix)
        $prefix = $null
        :candidates for ($second = 31; $second -ge 16; $second--) {
            for ($third = 254; $third -ge 0; $third--) {
                $candidate = "172.$second.$third.0/24"
                if (!@($routes | Where-Object {Test-SandboxPrefixOverlap $candidate $_}).Count) { $prefix = $candidate; break candidates }
            }
        }
        if (!$prefix) { throw 'No non-overlapping sandbox NAT prefix is available in 172.16.0.0/12.' }
        $network = [pscustomobject]@{schema='codex-permissive-network/v1';prefix=$prefix;natName='CodexPermissive-NAT'}
        New-NetNat -Name $network.natName -InternalIPInterfaceAddressPrefix $prefix | Out-Null
        Write-SandboxJson $networkFile $network
    }
    $base = $network.prefix.Split('/')[0].Split('.')[0..2] -join '.'
    $switchName = 'CodexPermissive-' + $SandboxId.Substring(0, 8)
    $existingSwitch = Get-VMSwitch -Name $switchName -ErrorAction SilentlyContinue
    $existingAddress = $null
    if ($existingSwitch) {
        if ($OwnerVMId -eq [Guid]::Empty -or [string]$existingSwitch.SwitchType -ne 'Internal') { throw 'Cannot resume an unowned sandbox switch.' }
        $peers = @(Get-VMNetworkAdapter -All | Where-Object {!$_.IsManagementOs -and $_.SwitchId -eq $existingSwitch.Id -and $_.VMId -ne $OwnerVMId})
        if ($peers.Count) { throw 'Another VM uses the sandbox switch; refusing to claim it.' }
        $existingAddress = @(Get-NetIPAddress -InterfaceAlias "vEthernet ($switchName)" -AddressFamily IPv4 | Where-Object {$_.IPAddress -notlike '169.254.*'})
        if ($existingAddress.Count -gt 1) { throw 'Resumed sandbox switch has unexpected host addresses.' }
        if ($existingAddress.Count -eq 1 -and ($existingAddress[0].PrefixLength -ne 30 -or !(Test-SandboxPrefixOverlap $network.prefix ($existingAddress[0].IPAddress + '/32')))) { throw 'Resumed sandbox switch is outside the project NAT.' }
    }
    $used = @(Get-NetIPAddress -AddressFamily IPv4 | Select-Object -ExpandProperty IPAddress)
    $offset = $null
    if ($existingAddress -and $existingAddress.Count -eq 1) {
        $offset = [int]($existingAddress[0].IPAddress.Split('.')[-1]) - 1
        if (($offset % 4) -ne 0) { throw 'Resumed sandbox gateway is not the expected /30 host address.' }
    } else {
        for ($candidate = 0; $candidate -le 252; $candidate += 4) {
            $prefix = "$base.$candidate/30"
            if (!@($used | Where-Object {Test-SandboxPrefixOverlap $prefix ($_ + '/32')}).Count) { $offset = $candidate; break }
        }
    }
    if ($null -eq $offset) { throw 'The sandbox NAT has no unused /30 available.' }
    $gateway = "$base.$($offset + 1)"; $guest = "$base.$($offset + 2)"
    if (!$existingSwitch) { New-VMSwitch -Name $switchName -SwitchType Internal | Out-Null }
    $interface = Get-NetAdapter -Name "vEthernet ($switchName)"
    if (!$existingAddress -or !$existingAddress.Count) { New-NetIPAddress -InterfaceIndex $interface.ifIndex -IPAddress $gateway -PrefixLength 30 | Out-Null }
    $deny = @(Get-SandboxDenyPrefixes) + @($PublicEgressAddress + '/32')
    $deny += @(Get-NetRoute -AddressFamily IPv4 | Where-Object {$_.DestinationPrefix -ne '0.0.0.0/0'} | Select-Object -ExpandProperty DestinationPrefix)
    $deny += @(Get-NetIPAddress -AddressFamily IPv4 | ForEach-Object {$_.IPAddress + '/32'})
    Get-NetFirewallRule -Name ('CodexPermissive-Relay-' + $SandboxId) -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -Name ('CodexPermissive-Relay-' + $SandboxId) -DisplayName 'Codex Permissive inference relay' -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $gateway -LocalPort 18445 -RemoteAddress $guest -InterfaceAlias $interface.Name -Profile Any | Out-Null
    return [ordered]@{guestIp=$guest;gatewayIp=$gateway;prefix="$base.$offset/30";natPrefix=$network.prefix;switchName=$switchName;natName=$network.natName;denyPrefixes=@($deny | Sort-Object -Unique);publicEgressAddress=$PublicEgressAddress;relayListenUrl="https://${gateway}:18445"}
}

function Wait-SandboxSession([Guid]$VMId, [Management.Automation.PSCredential]$Credential, [int]$TimeoutSeconds = 1200) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        try { return New-PSSession -VMId $VMId -Credential $Credential -ErrorAction Stop } catch { Start-Sleep -Seconds 5 }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'PowerShell Direct did not become ready. Guest remains disconnected. Inspect its console and rerun the completion helper.'
}

function Initialize-SandboxGuest($Binding, [string]$RelayCertificatePath = '', [int]$TimeoutSeconds = 1200) {
    $credential = Get-SandboxCredential $Binding
    $session = Wait-SandboxSession ([Guid]$Binding.vmId) $credential $TimeoutSeconds
    try {
        $certificate = ''
        if ($RelayCertificatePath) {
            Copy-Item -LiteralPath $RelayCertificatePath -Destination 'C:\ProgramData\CodexPermissive\relay-ca.pem' -ToSession $session -Force
            $certificate = 'C:\ProgramData\CodexPermissive\relay-ca.pem'
        }
        $adapter = Get-VM -Id ([Guid]$Binding.vmId) | Get-VMNetworkAdapter
        $deployment = @{schema='codex-permissive-deployment/v1';role='guest';sandboxId=$Binding.sandboxId;packagePath='C:\CodexPermissive';inferenceEndpoint='https://permissive-gateway.internal:18445/v1';relayCertificatePath='C:\ProgramData\CodexPermissive\relay-ca.pem'} | ConvertTo-Json -Compress
        Invoke-Command -Session $session -ScriptBlock {
            param($GuestIp, $GatewayIp, $Mac, $Deployment, $GuestCredential, $Certificate)
            & 'C:\CodexPermissive\windows\sandbox\Initialize-Guest.ps1' -GuestIp $GuestIp -GatewayIp $GatewayIp -MacAddress $Mac -DeploymentJson $Deployment -GuestCredential $GuestCredential -RelayCertificatePath $Certificate
        } -ArgumentList $Binding.guestIp, $Binding.gatewayIp, $adapter.MacAddress, $deployment, $credential, $certificate | Out-Null
    } finally { Remove-PSSession -Session $session }
}

function Initialize-SandboxRelay($Binding, [string]$BindingPath) {
    $python = Join-Path $Binding.packagePath 'python\python.exe'
    $result = & $python -m permissive.inference_relay --deployment $BindingPath --initialize-tls
    if ($LASTEXITCODE) { throw 'Host inference relay TLS initialization failed.' }
    $tls = ($result -join "`n") | ConvertFrom-Json
    return [string]$tls.certificatePath
}

function Install-SandboxControllerBinding([string]$BindingPath, [string]$PackageDirectory) {
    $target = Join-Path $PackageDirectory 'deployment.json'
    if ([IO.Path]::GetFullPath($target) -ne [IO.Path]::GetFullPath($BindingPath)) { Copy-Item -LiteralPath $BindingPath -Destination $target -Force }
    $binding = Read-SandboxBinding $BindingPath
    $taskName = 'CodexPermissiveController-' + ([Guid]$binding.sandboxId).ToString()
    $script = Join-Path $PackageDirectory 'windows\sandbox\Start-Controller.ps1'
    $arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $script + '" -BindingPath "' + [IO.Path]::GetFullPath($BindingPath) + '"'
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $executable = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (!$task -or @($task.Actions).Count -ne 1 -or $task.Actions.Execute -ne $executable -or $task.Actions.Arguments -ne $arguments -or $task.Principal.UserId -ne $identity -or $task.Principal.RunLevel -ne 'Highest' -or $task.Principal.LogonType -ne 'Interactive') {
        $action = New-ScheduledTaskAction -Execute $executable -Argument $arguments
        $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Highest
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Force | Out-Null
    }
}

function Get-SandboxTransportPassword([Security.SecureString]$TransportPassword, [switch]$TransportPasswordStdin) {
    if ($TransportPasswordStdin) {
        $line = [Console]::In.ReadLine()
        if (!$line) { throw 'A transport passphrase must be supplied through stdin.' }
        try { return ConvertTo-SecureString $line -AsPlainText -Force } finally { $line = $null }
    }
    if ($TransportPassword) { return $TransportPassword }
    if ([Console]::IsInputRedirected) { throw 'Supply a transport passphrase using -TransportPassword or -TransportPasswordStdin.' }
    return Read-Host -Prompt 'Portable bundle passphrase' -AsSecureString
}

function Invoke-SandboxSecretCodec([string]$PackageDirectory, [ValidateSet('seal','open')][string]$Operation, $Value, [Security.SecureString]$Password) {
    $credential = New-Object Management.Automation.PSCredential('transport', $Password)
    $inputData = @{operation=$Operation;value=$Value;password=$credential.GetNetworkCredential().Password} | ConvertTo-Json -Depth 12 -Compress
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = Join-Path $PackageDirectory 'python\python.exe'
    # Fixed code and stdin data avoid command-line quoting and secret disclosure.
    $start.Arguments = '-c "import json,sys; from permissive.portability import seal_secret,open_secret; r=json.load(sys.stdin); print(json.dumps(seal_secret(r[''value''],r[''password'']) if r[''operation'']==''seal'' else open_secret(r[''value''],r[''password''])))"'
    $start.UseShellExecute = $false; $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true; $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = [Text.Encoding]::UTF8
    $start.EnvironmentVariables['PYTHONUTF8'] = '1'
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    try {
        if (!$process.Start()) { throw 'Portable credential codec could not start.' }
        $errors = $process.StandardError.ReadToEndAsync()
        $inputBytes = [Text.Encoding]::UTF8.GetBytes($inputData)
        try { $process.StandardInput.BaseStream.Write($inputBytes,0,$inputBytes.Length); $process.StandardInput.Close() } finally { [Array]::Clear($inputBytes,0,$inputBytes.Length); $inputData=$null }
        $result = $process.StandardOutput.ReadToEnd()
        $process.WaitForExit(); $null = $errors.GetAwaiter().GetResult()
        if ($process.ExitCode) { throw 'Portable credential could not be authenticated/decrypted with this passphrase.' }
        return $result | ConvertFrom-Json
    } finally { $inputData = $null; $process.Dispose() }
}
