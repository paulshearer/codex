# Local-owner companion setup; never called through the network API.
[CmdletBinding()]
param([ValidateSet('Status','Pair','Revoke','Disable','Ensure','Run','Fence')][string]$Action = 'Status',
      [string]$PackageDirectory,
      [string]$HomeDirectory,
      [string]$PythonPath,
      [string]$SourcePath,
      [string]$ApiUrl = 'http://127.0.0.1:8789',
      [string]$DeviceId)
$ErrorActionPreference = 'Stop'
if (!$PackageDirectory) { $PackageDirectory = Split-Path $PSScriptRoot -Parent }
$PackageDirectory = [IO.Path]::GetFullPath($PackageDirectory)
if (!$HomeDirectory) { $HomeDirectory = Join-Path $env:LOCALAPPDATA 'CodexPermissive' }
$HomeDirectory = [IO.Path]::GetFullPath($HomeDirectory)
if (!$PythonPath) { $PythonPath = Join-Path $PackageDirectory 'python/python.exe' }
if (!$SourcePath) { $SourcePath = Join-Path $PackageDirectory 'lib' }
$homeBindingPath = Join-Path $PackageDirectory 'companion-home.json'
if (Test-Path -LiteralPath $homeBindingPath -PathType Leaf) {
    try { $homeBinding = Get-Content -LiteralPath $homeBindingPath -Raw | ConvertFrom-Json }
    catch { throw 'The local companion data binding is invalid.' }
    if (@($homeBinding.PSObject.Properties).Count -ne 3 -or $homeBinding.version -ne 1 -or
        $homeBinding.logicalHome -isnot [string] -or $homeBinding.physicalHome -isnot [string] -or
        ![IO.Path]::IsPathRooted($homeBinding.logicalHome) -or ![IO.Path]::IsPathRooted($homeBinding.physicalHome) -or
        ($HomeDirectory -ine [IO.Path]::GetFullPath($homeBinding.logicalHome) -and $HomeDirectory -ine [IO.Path]::GetFullPath($homeBinding.physicalHome))) {
        throw 'The companion data binding differs from this Windows installation.'
    }
    $HomeDirectory = [IO.Path]::GetFullPath($homeBinding.physicalHome)
}
# A Store-packaged caller can redirect individual AppData files while a native
# scheduled task sees the original directory. Resolve existing files, rather
# than their parent directories, before configuring either side of the task.
$pathResolver = @'
import json,sys
from pathlib import Path
package,home,python,source=map(Path,sys.argv[1:])
history=home/'tasks.sqlite'
state=home/'companion'/'state.sqlite'
if history.is_file(): home=history.resolve().parent
elif state.is_file(): home=state.resolve().parent.parent
else: home=home.resolve()
helper=package/'windows'/'mobile-companion.ps1'
module=source/'permissive'/'mobile.py'
print(json.dumps({'package':str(helper.resolve().parent.parent),'home':str(home),'python':str(python.resolve()),'source':str(module.resolve().parent.parent)}))
'@
$resolvedPaths = & $PythonPath -B -X utf8 -c $pathResolver $PackageDirectory $HomeDirectory $PythonPath $SourcePath
if ($LASTEXITCODE -ne 0) { throw 'The companion paths could not be resolved.' }
$resolvedPaths = ($resolvedPaths -join "`n") | ConvertFrom-Json
$PackageDirectory = $resolvedPaths.package
$HomeDirectory = $resolvedPaths.home
$PythonPath = $resolvedPaths.python
$SourcePath = $resolvedPaths.source
$env:PYTHONPATH = $SourcePath
$env:PYTHONUTF8 = '1'
$env:RUST_LOG = 'off'
$homeHash = [Security.Cryptography.SHA256]::Create()
try { $taskSuffix = ([BitConverter]::ToString($homeHash.ComputeHash([Text.Encoding]::UTF8.GetBytes($HomeDirectory.ToLowerInvariant())))).Replace('-','').Substring(0,16) }
finally { $homeHash.Dispose() }
$taskName = 'CodexPermissiveCompanion-' + $taskSuffix

function Invoke-Mobile([string[]]$Parameters) {
    $result = & $PythonPath -B -X utf8 -m permissive.mobile --home $HomeDirectory @Parameters
    if ($LASTEXITCODE -ne 0) { throw 'The local companion operation failed.' }
    return (($result -join "`n") | ConvertFrom-Json)
}
function Get-ExistingServiceOptIn([string]$Directory, [string]$Origin) {
    $connectionPath = Join-Path $Directory 'client-connection.json'
    if (!(Test-Path -LiteralPath $connectionPath -PathType Leaf)) { return $false }
    try { $connection = Get-Content -LiteralPath $connectionPath -Raw | ConvertFrom-Json }
    catch { throw 'The local client connection configuration is invalid.' }
    if (@($connection.PSObject.Properties).Count -ne 2 -or $connection.mode -isnot [string] -or $connection.origin -isnot [string] -or $connection.mode -cne 'existing-service' -or
        $connection.origin -cne 'http://127.0.0.1:8789' -or $Origin.TrimEnd('/') -cne $connection.origin) {
        throw 'The local client connection must select the existing loopback Windows service.'
    }
    return $true
}
function Get-ServeConfiguration {
    $raw = & tailscale.exe serve status --json
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Tailscale Serve.' }
    return (($raw -join "`n") | ConvertFrom-Json)
}
function Test-OwnedServe($Configuration, [string]$Origin) {
    $authority = ([Uri]$Origin).Authority
    if ($Configuration.AllowFunnel -and $Configuration.AllowFunnel.PSObject.Properties[$authority] -and $Configuration.AllowFunnel.PSObject.Properties[$authority].Value) { throw 'Public Funnel exposure must be disabled for the companion.' }
    if ($Configuration.Foreground) {
        foreach ($foreground in $Configuration.Foreground.PSObject.Properties.Value) {
            if ($foreground.TCP -and $foreground.TCP.PSObject.Properties['9444']) { throw 'Tailscale port 9444 has a foreground owner.' }
        }
    }
    $tcpEntry = if ($Configuration.TCP) { $Configuration.TCP.PSObject.Properties['9444'] } else { $null }
    $webEntry = if ($Configuration.Web) { $Configuration.Web.PSObject.Properties[$authority] } else { $null }
    if (!$webEntry) {
        if ($tcpEntry) { throw 'Tailscale port 9444 is already assigned to another application.' }
        return $false
    }
    $handlers = @($webEntry.Value.Handlers.PSObject.Properties)
    if (!$tcpEntry -or !$tcpEntry.Value.HTTPS -or $tcpEntry.Value.TCPForward -or $handlers.Count -ne 1 -or $handlers[0].Name -ne '/' -or $handlers[0].Value.Proxy -ne 'http://127.0.0.1:8790') { throw 'Tailscale port 9444 is already assigned to another application.' }
    return $true
}
function Stop-Companion($State) {
    if ($State.enabled) { $null = Invoke-Mobile -Parameters @('disable') }
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) { Stop-ScheduledTask -TaskName $taskName; Disable-ScheduledTask -TaskName $taskName | Out-Null }
    if ($State.origin -and (Get-Command tailscale.exe -ErrorAction SilentlyContinue)) {
        $serve = Get-ServeConfiguration
        if (Test-OwnedServe $serve $State.origin) {
            & tailscale.exe serve --https=9444 off | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Companion disabled, but its Tailscale listener could not be removed.' }
        }
    }
}
function Start-Companion {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $scriptPath = Join-Path $PackageDirectory 'windows/mobile-companion.ps1'
    foreach ($path in @($scriptPath,$PackageDirectory,$HomeDirectory,$PythonPath,$SourcePath)) {
        if ($path.Contains('"')) { throw 'A companion path contains an unsupported quote.' }
    }
    $arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $scriptPath + '" -Action Run -PackageDirectory "' + $PackageDirectory + '" -HomeDirectory "' + $HomeDirectory + '" -PythonPath "' + $PythonPath + '" -SourcePath "' + $SourcePath + '"'
    $executable = Join-Path $env:WINDIR 'System32/WindowsPowerShell/v1.0/powershell.exe'
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $hasRecovery = $task -and @($task.Triggers | Where-Object { $_.Repetition.Interval -eq 'PT1M' }).Count -gt 0
    if (!$task -or @($task.Actions).Count -ne 1 -or $task.Actions.Execute -ne $executable -or $task.Actions.Arguments -ne $arguments -or $task.Principal.UserId -ne $identity -or $task.Principal.RunLevel -ne 'Limited' -or $task.Principal.LogonType -ne 'Interactive' -or !$hasRecovery -or $task.Settings.RestartCount -ne 999) {
        if ($task -and $task.State -eq 'Running') { Stop-ScheduledTask -TaskName $taskName }
        $taskAction = New-ScheduledTaskAction -Execute $executable -Argument $arguments
        $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
        # Retry after a late backend/Tailscale startup and after clean exits.
        # An already running companion is left alone by IgnoreNew.
        $recovery = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Principal $principal -Trigger @($trigger,$recovery) -Settings $settings -Force | Out-Null
    }
    Enable-ScheduledTask -TaskName $taskName | Out-Null
    if ((Get-ScheduledTask -TaskName $taskName).State -ne 'Running') { Start-ScheduledTask -TaskName $taskName }
}

if ($Action -eq 'Run') {
    if ($HomeDirectory.Contains('"')) { throw 'An unsupported quote was found in the companion data path.' }
    $runInfo = New-Object Diagnostics.ProcessStartInfo
    $runInfo.FileName = $PythonPath
    $runInfo.Arguments = '-B -X utf8 -m permissive.mobile --home "' + $HomeDirectory + '" serve'
    $runInfo.UseShellExecute = $false
    $runInfo.CreateNoWindow = $true
    $runInfo.RedirectStandardOutput = $true
    $runInfo.RedirectStandardError = $true
    $runProcess = New-Object Diagnostics.Process
    $runProcess.StartInfo = $runInfo
    if (!$runProcess.Start()) { throw 'The companion process could not start.' }
    # PowerShell 5.1 can promote native stderr to a terminating error. Drain both
    # pipes without publishing content and let the child's exit code decide.
    $runOutput = $runProcess.StandardOutput.BaseStream.CopyToAsync([IO.Stream]::Null)
    $runError = $runProcess.StandardError.BaseStream.CopyToAsync([IO.Stream]::Null)
    $runProcess.WaitForExit()
    $null = $runOutput.GetAwaiter().GetResult()
    $null = $runError.GetAwaiter().GetResult()
    exit $runProcess.ExitCode
}
$state = Invoke-Mobile -Parameters @('status')
switch ($Action) {
    'Status' { $state | ConvertTo-Json -Depth 8 -Compress }
    'Revoke' {
        if ($DeviceId -notmatch '^[0-9a-f]{64}$') { throw 'Invalid enrolled device identity.' }
        Invoke-Mobile -Parameters @('revoke',$DeviceId) | ConvertTo-Json -Depth 8 -Compress
    }
    { $_ -in 'Disable','Fence' } {
        Stop-Companion $state
        Invoke-Mobile -Parameters @('status') | ConvertTo-Json -Depth 8 -Compress
    }
    'Ensure' {
        if ($state.enabled) { Start-Companion }
        @{enabled=[bool]$state.enabled} | ConvertTo-Json -Compress
    }
    'Pair' {
        $existingService = Get-ExistingServiceOptIn $PackageDirectory $ApiUrl
        $tailnetRaw = & tailscale.exe status --json
        if ($LASTEXITCODE -ne 0) { throw 'Tailscale must be installed and connected.' }
        $tailnet = ($tailnetRaw -join "`n") | ConvertFrom-Json
        $dnsName = ([string]$tailnet.Self.DNSName).TrimEnd('.').ToLowerInvariant()
        if ($tailnet.BackendState -ne 'Running' -or $dnsName -notmatch '^[a-z0-9-]+\.[a-z0-9.-]+\.ts\.net$') { throw 'Connect this Windows machine to Tailscale first.' }
        $origin = 'https://' + $dnsName + ':9444'
        $serve = Get-ServeConfiguration
        $null = Test-OwnedServe $serve $origin
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 8790 -ErrorAction SilentlyContinue)
        if ($listeners.Count -and !$state.running) { throw 'Loopback port 8790 is already assigned to another process or installation.' }
        if ($state.running -and ($state.origin -ne $origin -or $state.upstream -ne $ApiUrl.TrimEnd('/') -or $state.port -ne 8790 -or [bool]$state.existingService -ne $existingService)) {
            Stop-ScheduledTask -TaskName $taskName
        }
        $configureParameters = @('configure','--origin',$origin,'--upstream',$ApiUrl,'--port','8790')
        if ($existingService) { $configureParameters += '--existing-service' }
        $null = Invoke-Mobile -Parameters $configureParameters
        & tailscale.exe serve --bg --https=9444 http://127.0.0.1:8790 | Out-Null
        if ($LASTEXITCODE -ne 0) { $null = Invoke-Mobile -Parameters @('disable'); throw 'Tailscale companion exposure failed.' }
        try {
            Start-Companion
            $ready = $false
            for ($attempt = 0; $attempt -lt 30; $attempt++) {
                $check = Invoke-Mobile -Parameters @('status')
                if ($check.running) { $ready = $true; break }
                Start-Sleep -Milliseconds 500
            }
            if (!$ready) { throw 'The companion could not connect to its Windows service.' }
        } catch { Stop-Companion (Invoke-Mobile -Parameters @('status')); throw }
        Invoke-Mobile -Parameters @('invite') | ConvertTo-Json -Depth 8 -Compress
    }
}
