#requires -Version 5.1
<#
Runs only when explicitly invoked. Run with the guest agent idle. No production
service ports are probed: private TCP witnesses are owned by this invocation.
#>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$BindingPath,
      [ValidateRange(2000,3000)][int]$ProbeTimeoutMilliseconds = 2500)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SandboxAdministrator
$binding = Read-SandboxBinding $BindingPath
$checks = New-Object Collections.Generic.List[object]
$listeners = New-Object Collections.Generic.List[object]
$ruleNames = New-Object Collections.Generic.List[string]
$session = $null; $snapshot = $null; $spec = $null; $route = $null
$cleanup = [ordered]@{guestRestored=$false;hostRouteRemoved=$true;hostFirewallRemoved=$true;listenersStopped=$true}

function Add-IsolationCheck([string]$Name, [string]$Status, $Evidence) {
    $checks.Add([pscustomobject]@{name=$Name;status=$Status;evidence=$Evidence})
}

$restoreGuest = {
    param($Original, $ProbeSpec)
    $ErrorActionPreference = 'Stop'
    $adapter = @(Get-NetAdapter | Where-Object {($_.MacAddress -replace '[-:]','') -eq $ProbeSpec.mac})
    if ($adapter.Count -ne 1) { throw 'Guest adapter changed during isolation checks.' }
    if ($ProbeSpec.spoofIp -and $Original.ipv4 -notcontains $ProbeSpec.spoofIp) {
        Get-NetIPAddress -InterfaceIndex $adapter[0].ifIndex -AddressFamily IPv4 -IPAddress $ProbeSpec.spoofIp -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:$false
    }
    $v6 = Get-NetAdapterBinding -Name $adapter[0].Name -ComponentID ms_tcpip6
    if ([bool]$v6.Enabled -ne [bool]$Original.ipv6Enabled) {
        if ($Original.ipv6Enabled) { Enable-NetAdapterBinding -Name $adapter[0].Name -ComponentID ms_tcpip6 | Out-Null }
        else { Disable-NetAdapterBinding -Name $adapter[0].Name -ComponentID ms_tcpip6 | Out-Null }
    }
    foreach ($profile in $Original.firewall) { Set-NetFirewallProfile -Name $profile.Name -Enabled ([bool]$profile.Enabled) }
    foreach ($profile in $Original.firewall) {
        if ([bool](Get-NetFirewallProfile -Name $profile.Name).Enabled -ne [bool]$profile.Enabled) { throw 'Guest firewall restoration could not be verified.' }
    }
    if ([bool](Get-NetAdapterBinding -Name $adapter[0].Name -ComponentID ms_tcpip6).Enabled -ne [bool]$Original.ipv6Enabled) { throw 'Guest IPv6 restoration could not be verified.' }
    if ($ProbeSpec.spoofIp -and $Original.ipv4 -notcontains $ProbeSpec.spoofIp -and @(Get-NetIPAddress -InterfaceIndex $adapter[0].ifIndex -IPAddress $ProbeSpec.spoofIp -ErrorAction SilentlyContinue).Count) { throw 'Temporary guest source address remains configured.' }
    return $true
}

$guestProbes = {
    param($ProbeSpec, $Original)
    $ErrorActionPreference = 'Stop'
    $results = New-Object Collections.Generic.List[object]
    function Record([string]$Name, [string]$Status, $Evidence) { $results.Add([pscustomobject]@{name=$Name;status=$Status;evidence=$Evidence}) }
    function TcpProbe([string]$Address, [int]$Port, [string]$Source = '') {
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $client = $null; $sourceBound = $false
        try {
            $ip = [Net.IPAddress]::Parse($Address)
            $client = New-Object Net.Sockets.TcpClient($ip.AddressFamily)
            if ($Source) { $client.Client.Bind((New-Object Net.IPEndPoint([Net.IPAddress]::Parse($Source),0))); $sourceBound=$true }
            $pending = $client.ConnectAsync($ip, $Port)
            if (!$pending.Wait($ProbeSpec.timeoutMs)) { return @{connected=$false;outcome='timeout';elapsedMs=$watch.ElapsedMilliseconds} }
            $pending.GetAwaiter().GetResult()
            return @{connected=[bool]$client.Connected;outcome='connected';elapsedMs=$watch.ElapsedMilliseconds}
        } catch { return @{connected=$false;outcome=$(if ($Source -and !$sourceBound) {'source_bind_failed'} else {'socket_error'});elapsedMs=$watch.ElapsedMilliseconds} }
        finally { if ($client) {$client.Dispose()} }
    }
    try {
        Set-NetFirewallProfile -Profile Domain,Private,Public -Enabled False
        if (@(Get-NetFirewallProfile | Where-Object {$_.Enabled}).Count) { throw 'Guest firewall could not be disabled.' }
        Record 'guest_firewall_disabled' 'passed' @{profiles=@('Domain','Private','Public')}
        $public = TcpProbe '1.1.1.1' 443
        Record 'public_tcp_443' $(if ($public.connected) {'passed'} else {'failed'}) $public

        $dns = $null; $dnsPass = $false; $watch = [Diagnostics.Stopwatch]::StartNew()
        try {
            $dns = New-Object Net.Sockets.UdpClient
            $dns.Client.ReceiveTimeout = $ProbeSpec.timeoutMs
            $dns.Client.SendTimeout = $ProbeSpec.timeoutMs
            $dns.Connect('1.1.1.1',53)
            $query = [byte[]]@(0x50,0x4d,1,0,0,1,0,0,0,0,0,0,7)
            $query += [Text.Encoding]::ASCII.GetBytes('example')
            $query += [byte[]]@(3)
            $query += [Text.Encoding]::ASCII.GetBytes('com')
            $query += [byte[]]@(0,0,1,0,1)
            $null = $dns.Send($query,$query.Length)
            $peer = New-Object Net.IPEndPoint([Net.IPAddress]::Any,0)
            $answer = $dns.Receive([ref]$peer)
            $dnsPass = $answer.Length -ge 12 -and $answer[0] -eq 0x50 -and $answer[1] -eq 0x4d -and ($answer[2] -band 0x80) -ne 0 -and ($answer[3] -band 0x0f) -eq 0 -and ($answer[6] -ne 0 -or $answer[7] -ne 0)
        } catch { $dnsPass = $false } finally { if ($dns) {$dns.Dispose()} }
        Record 'public_dns' $(if ($dnsPass) {'passed'} else {'failed'}) @{server='1.1.1.1';query='example.com';elapsedMs=$watch.ElapsedMilliseconds}

        Add-Type -AssemblyName System.Net.Http
        $handler = New-Object Net.Http.HttpClientHandler
        $handler.AllowAutoRedirect = $false; $handler.UseProxy = $false
        $handler.SslProtocols = [Security.Authentication.SslProtocols]::Tls12
        $http = New-Object Net.Http.HttpClient($handler)
        $http.Timeout = [TimeSpan]::FromMilliseconds($ProbeSpec.timeoutMs)
        $status = $null; $relayPass = $false
        try {
            $request = $http.GetAsync('https://permissive-gateway.internal:18445/v1/models',[Net.Http.HttpCompletionOption]::ResponseHeadersRead)
            if ($request.Wait($ProbeSpec.timeoutMs)) {
                $response = $request.GetAwaiter().GetResult()
                try { $status = [int]$response.StatusCode; $relayPass = $status -eq 200 } finally {$response.Dispose()}
            }
        } catch { $relayPass = $false } finally { $http.CancelPendingRequests(); $http.Dispose(); $handler.Dispose() }
        Record 'inference_models_tls' $(if ($relayPass) {'passed'} else {'failed'}) @{statusCode=$status;certificateValidation=$true}

        foreach ($target in $ProbeSpec.targets) {
            $probe = TcpProbe $target.address 18446
            Record $target.name $(if (!$probe.connected) {'passed'} else {'failed'}) @{address=$target.address;port=18446;hostListenerVerified=$true;probe=$probe}
        }
        $metadata = TcpProbe '169.254.169.254' 80
        Record 'metadata_unreachable' $(if (!$metadata.connected) {'passed'} else {'failed'}) @{address='169.254.169.254';port=80;probe=$metadata;proof='Host ACL plus reachability check; no metadata endpoint witness was created'}

        $adapter = @(Get-NetAdapter | Where-Object {($_.MacAddress -replace '[-:]','') -eq $ProbeSpec.mac})[0]
        if ($ProbeSpec.spoofIp -and $public.connected) {
            New-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress $ProbeSpec.spoofIp -PrefixLength 32 -SkipAsSource $true | Out-Null
            $deadline = [DateTime]::UtcNow.AddMilliseconds($ProbeSpec.timeoutMs)
            do {
                $address = Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -IPAddress $ProbeSpec.spoofIp
                if ([string]$address.AddressState -eq 'Preferred') {break}
                Start-Sleep -Milliseconds 100
            } while ([DateTime]::UtcNow -lt $deadline)
            if ([string]$address.AddressState -eq 'Preferred') {
                $spoof = TcpProbe '1.1.1.1' 443 $ProbeSpec.spoofIp
                Record 'source_ip_pin' $(if ($spoof.outcome -eq 'source_bind_failed') {'untested'} elseif (!$spoof.connected) {'passed'} else {'failed'}) @{source=$ProbeSpec.spoofIp;hostReturnRoute=$true;publicBaseline=$true;probe=$spoof}
            } else { Record 'source_ip_pin' 'untested' @{reason='Temporary source address did not become usable within its deadline'} }
        } else { Record 'source_ip_pin' 'untested' @{reason='No unused source address or no working public baseline'} }

        if ($ProbeSpec.ipv6Address) {
            if (!(Get-NetAdapterBinding -Name $adapter.Name -ComponentID ms_tcpip6).Enabled) { Enable-NetAdapterBinding -Name $adapter.Name -ComponentID ms_tcpip6 | Out-Null }
            $deadline = [DateTime]::UtcNow.AddMilliseconds($ProbeSpec.timeoutMs)
            do {
                $v6 = @(Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv6 -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -like 'fe80:*' -and [string]$_.AddressState -eq 'Preferred'})
                if ($v6.Count) {break}
                Start-Sleep -Milliseconds 100
            } while ([DateTime]::UtcNow -lt $deadline)
            if ($v6.Count) {
                $probe = TcpProbe ($ProbeSpec.ipv6Address + '%' + $adapter.ifIndex) 18446
                Record 'ipv6_isolation' $(if (!$probe.connected) {'passed'} else {'failed'}) @{hostListenerVerified=$true;probe=$probe}
            } else { Record 'ipv6_isolation' 'untested' @{reason='Guest IPv6 link-local address did not become usable within its deadline'} }
        } else { Record 'ipv6_isolation' 'untested' @{reason='No usable host IPv6 witness on the dedicated switch'} }
        Record 'mac_spoof_mutation' 'untested' @{reason='MAC mutation would invalidate adapter identity; host MacAddressSpoofing=Off was verified'}
    } catch { Record 'guest_probe_execution' 'failed' @{reason='A fixed isolation probe could not complete'} }
    finally {
        try {
            $restoreAdapter=@(Get-NetAdapter | Where-Object {($_.MacAddress -replace '[-:]','') -eq $ProbeSpec.mac})[0]
            if ($ProbeSpec.spoofIp -and $Original.ipv4 -notcontains $ProbeSpec.spoofIp) {Get-NetIPAddress -InterfaceIndex $restoreAdapter.ifIndex -AddressFamily IPv4 -IPAddress $ProbeSpec.spoofIp -ErrorAction SilentlyContinue | Remove-NetIPAddress -Confirm:$false}
            if ($Original.ipv6Enabled) {Enable-NetAdapterBinding -Name $restoreAdapter.Name -ComponentID ms_tcpip6 | Out-Null}
            else {Disable-NetAdapterBinding -Name $restoreAdapter.Name -ComponentID ms_tcpip6 | Out-Null}
            foreach ($profile in $Original.firewall) {Set-NetFirewallProfile -Name $profile.Name -Enabled ([bool]$profile.Enabled)}
            Record 'guest_restore' 'passed' @{}
        }
        catch { Record 'guest_restore' 'failed' @{reason='Host emergency restoration is required'} }
    }
    return [pscustomobject]@{checks=$results.ToArray()}
}

try {
    Test-SandboxPerimeter $binding | Out-Null
    Add-IsolationCheck 'host_perimeter' 'passed' @{vmId=$binding.vmId;switch=$binding.switchName}
    $vm = Get-VM -Id ([Guid]$binding.vmId)
    if ($vm.State -ne 'Running') { throw 'Isolation acceptance requires a running guest.' }
    $adapter = $vm | Get-VMNetworkAdapter
    $credential = Get-SandboxCredential $binding
    $session = New-PSSession -VMId $vm.Id -Credential $credential
    $snapshot = Invoke-Command -Session $session -ScriptBlock {
        param($Mac)
        $nic = @(Get-NetAdapter | Where-Object {($_.MacAddress -replace '[-:]','') -eq $Mac})
        if ($nic.Count -ne 1) {throw 'Guest adapter cannot be identified'}
        [pscustomobject]@{firewall=@(Get-NetFirewallProfile | Select-Object Name,Enabled);ipv6Enabled=[bool](Get-NetAdapterBinding -Name $nic[0].Name -ComponentID ms_tcpip6).Enabled;ipv4=@(Get-NetIPAddress -InterfaceIndex $nic[0].ifIndex -AddressFamily IPv4 | Select-Object -ExpandProperty IPAddress)}
    } -ArgumentList ($adapter.MacAddress -replace '[-:]','')
    $interface = Get-NetAdapter -Name ("vEthernet (" + $binding.switchName + ')')
    $targets = New-Object Collections.Generic.List[object]
    $descriptors = @([pscustomobject]@{name='gateway_private_isolation';address=$binding.gatewayIp})
    $local = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.AddressState -eq 'Preferred'})
    $lan = @($local | Where-Object {$_.InterfaceAlias -notlike 'vEthernet*' -and $_.InterfaceAlias -notlike '*Tailscale*' -and ($_.IPAddress -like '10.*' -or $_.IPAddress -like '192.168.*' -or (Test-SandboxPrefixOverlap '172.16.0.0/12' ($_.IPAddress + '/32')))} | Select-Object -First 1)
    $tailnet = @($local | Where-Object {Test-SandboxPrefixOverlap '100.64.0.0/10' ($_.IPAddress + '/32')} | Select-Object -First 1)
    if ($lan.Count) {$descriptors += [pscustomobject]@{name='host_lan_isolation';address=$lan[0].IPAddress}} else {Add-IsolationCheck 'host_lan_isolation' 'untested' @{reason='No host-owned private LAN address was available'}}
    if ($tailnet.Count) {$descriptors += [pscustomobject]@{name='host_tailnet_isolation';address=$tailnet[0].IPAddress}} else {Add-IsolationCheck 'host_tailnet_isolation' 'untested' @{reason='No host-owned Tailscale address was available'}}
    foreach ($target in $descriptors) {
        $listener = $null
        try {
            $ip = [Net.IPAddress]::Parse($target.address)
            $listener = New-Object Net.Sockets.TcpListener($ip,18446)
            $listener.Start(16)
            $witness = New-Object Net.Sockets.TcpClient
            try {
                $pending = $witness.ConnectAsync($ip,18446)
                if (!$pending.Wait($ProbeTimeoutMilliseconds)) {throw 'Host witness readiness timed out'}
                $pending.GetAwaiter().GetResult()
            } finally {$witness.Dispose()}
            $listeners.Add($listener); $listener=$null
            $targets.Add($target)
        } catch {
            if ($target.name -eq 'gateway_private_isolation') {throw 'Could not create the required private gateway witness.'}
            Add-IsolationCheck $target.name 'untested' @{reason='Owned host listener could not bind; no existing service was contacted'}
        } finally {if ($listener) {$listener.Stop()}}
    }
    $ruleName = 'CodexPermissive-Isolation-' + [Guid]::NewGuid().ToString()
    New-NetFirewallRule -Name $ruleName -DisplayName 'Temporary sandbox isolation witness' -Direction Inbound -Action Allow -Protocol TCP -LocalAddress @($targets | ForEach-Object {$_.address}) -LocalPort 18446 -RemoteAddress $binding.guestIp -InterfaceAlias $interface.Name -Profile Any | Out-Null
    $ruleNames.Add($ruleName)
    $ipv6Address = ''
    $v6 = @(Get-NetIPAddress -InterfaceIndex $interface.ifIndex -AddressFamily IPv6 -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -like 'fe80:*' -and $_.AddressState -eq 'Preferred'} | Select-Object -First 1)
    if ($v6.Count) {
        $listener = $null
        try {
            $ip = [Net.IPAddress]::Parse($v6[0].IPAddress.Split('%')[0]); $ip.ScopeId=$interface.ifIndex
            $listener = New-Object Net.Sockets.TcpListener($ip,18446)
            $listener.Server.DualMode=$false; $listener.Start(16)
            $witness = New-Object Net.Sockets.TcpClient([Net.Sockets.AddressFamily]::InterNetworkV6)
            try {$pending=$witness.ConnectAsync($ip,18446); if (!$pending.Wait($ProbeTimeoutMilliseconds)) {throw 'IPv6 witness timeout'}; $pending.GetAwaiter().GetResult()} finally {$witness.Dispose()}
            $ipv6Address=$ip.ToString().Split('%')[0]
            $listeners.Add($listener); $listener=$null
            $ruleName='CodexPermissive-Isolation-'+[Guid]::NewGuid().ToString()
            New-NetFirewallRule -Name $ruleName -DisplayName 'Temporary sandbox IPv6 isolation witness' -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $ipv6Address -LocalPort 18446 -RemoteAddress 'fe80::/10' -InterfaceAlias $interface.Name -Profile Any | Out-Null
            $ruleNames.Add($ruleName)
        } catch {$ipv6Address=''} finally {if ($listener) {$listener.Stop()}}
    }
    $spoofIp = ''
    if ($binding.natPrefix -match '^([0-9]+\.[0-9]+\.[0-9]+)\.0/24$') {
        $natBase=$Matches[1]
        $used=@($local | ForEach-Object {$_.IPAddress}) + @($snapshot.ipv4)
        $used+=@(Get-VMNetworkAdapter -All | ForEach-Object {$_.IPAddresses})
        for ($last=254;$last -ge 2;$last-=4) {
            $candidate="$natBase.$last"
            if ($used -contains $candidate -or @(Get-NetRoute -DestinationPrefix ($candidate+'/32') -ErrorAction SilentlyContinue).Count) {continue}
            $route=New-NetRoute -DestinationPrefix ($candidate+'/32') -InterfaceIndex $interface.ifIndex -NextHop '0.0.0.0' -RouteMetric 1 -PolicyStore ActiveStore
            $spoofIp=$candidate; break
        }
    }
    $spec=[pscustomobject]@{mac=($adapter.MacAddress -replace '[-:]','');timeoutMs=$ProbeTimeoutMilliseconds;targets=$targets.ToArray();spoofIp=$spoofIp;ipv6Address=$ipv6Address}
    $result=Invoke-Command -Session $session -ScriptBlock $guestProbes -ArgumentList $spec,$snapshot
    foreach ($check in $result.checks) {$checks.Add($check)}
} catch {Add-IsolationCheck 'acceptance_execution' 'failed' @{reason='Host setup or the fixed guest probes could not complete'}}
finally {
    if ($snapshot -and $spec) {
        try {
            if (!$session -or $session.State -ne 'Opened') {$session=New-PSSession -VMId ([Guid]$binding.vmId) -Credential (Get-SandboxCredential $binding)}
            $null=Invoke-Command -Session $session -ScriptBlock $restoreGuest -ArgumentList $snapshot,$spec
            $cleanup.guestRestored=$true
        } catch {$cleanup.guestRestored=$false}
    } elseif (!$snapshot) {$cleanup.guestRestored=$true}
    else {$cleanup.guestRestored=$true} # No guest mutation occurs before spec construction.
    if ($route) {try {$route | Remove-NetRoute -Confirm:$false} catch {$cleanup.hostRouteRemoved=$false}}
    foreach ($name in $ruleNames) {try {Remove-NetFirewallRule -Name $name} catch {$cleanup.hostFirewallRemoved=$false}}
    foreach ($listener in $listeners) {try {$listener.Stop()} catch {$cleanup.listenersStopped=$false}}
    if ($session) {Remove-PSSession -Session $session -ErrorAction SilentlyContinue}
}
$failed=@($checks | Where-Object {$_.status -eq 'failed'}).Count -gt 0 -or @($cleanup.Values | Where-Object {!$_}).Count -gt 0
$untested=@($checks | Where-Object {$_.status -eq 'untested'}).Count
$evidence=[ordered]@{schema='codex-permissive-isolation-evidence/v1';sandboxId=$binding.sandboxId;vmId=$binding.vmId;timestampUtc=[DateTime]::UtcNow.ToString('o');result=$(if ($failed) {'failed'} elseif ($untested -gt 1) {'incomplete'} else {'passed'});checks=$checks.ToArray();cleanup=$cleanup;liveTest=$true}
$evidence | ConvertTo-Json -Depth 12
if ($failed) {exit 1}
