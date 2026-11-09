# Repair startup for an existing pre-VM backend without changing its runtime or data.
#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)][string]$PackageDirectory,
      [Parameter(Mandatory)][string]$HomeDirectory)
$ErrorActionPreference = 'Stop'
$PackageDirectory = (Resolve-Path -LiteralPath $PackageDirectory).Path
$HomeDirectory = (Resolve-Path -LiteralPath $HomeDirectory).Path
if (Test-Path -LiteralPath (Join-Path $PackageDirectory 'deployment.json')) {
    throw 'Use the registered Hyper-V controller task for a bound deployment.'
}
foreach ($relative in @('python/python.exe','lib/permissive/__main__.py','native/codex-app-server.exe','node/node.exe','browser/worker.mjs')) {
    if (!(Test-Path -LiteralPath (Join-Path $PackageDirectory $relative) -PathType Leaf)) { throw 'An existing backend runtime is required.' }
}
if (!(Test-Path -LiteralPath (Join-Path $HomeDirectory 'tasks.sqlite') -PathType Leaf)) { throw 'Select the existing application history directory.' }
# Preserve the physical AppData binding used by native scheduled-task processes.
$resolver = 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().parent)'
$HomeDirectory = (& (Join-Path $PackageDirectory 'python/python.exe') -B -c $resolver (Join-Path $HomeDirectory 'tasks.sqlite') | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or !$HomeDirectory) { throw 'The application history directory could not be resolved.' }
$hash = [Security.Cryptography.SHA256]::Create()
try { $suffix = ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($HomeDirectory.ToLowerInvariant())))).Replace('-','').Substring(0,16) }
finally { $hash.Dispose() }
$taskName = 'CodexPermissiveBackend-' + $suffix
$runner = Join-Path $PackageDirectory 'windows/run-existing-service.ps1'
$sourceRunner = Join-Path $PSScriptRoot 'run-existing-service.ps1'
if ([IO.Path]::GetFullPath($sourceRunner) -ine [IO.Path]::GetFullPath($runner)) { Copy-Item -LiteralPath $sourceRunner -Destination $runner -Force }
foreach ($path in @($runner,$PackageDirectory,$HomeDirectory)) {
    if ($path.Contains('"')) { throw 'An application path contains an unsupported quote.' }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$executable = Join-Path $env:WINDIR 'System32/WindowsPowerShell/v1.0/powershell.exe'
$arguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $runner + '" -PackageDirectory "' + $PackageDirectory + '" -HomeDirectory "' + $HomeDirectory + '"'
$action = New-ScheduledTaskAction -Execute $executable -Argument $arguments -WorkingDirectory $PackageDirectory
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$logon = New-ScheduledTaskTrigger -AtLogOn -User $identity
# The recurring trigger also recovers clean exits and exhausted failure retries.
# IgnoreNew prevents it from interrupting an already running service.
$recovery = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$backupDirectory = Join-Path $HomeDirectory ('backups/startup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
$oldTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($oldTask) { Export-ScheduledTask -TaskName $taskName | Set-Content -LiteralPath (Join-Path $backupDirectory 'backend-task.xml') -Encoding UTF8 }
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$oldRun = (Get-ItemProperty -LiteralPath $runKey -ErrorAction SilentlyContinue).CodexPermissive
@{name='CodexPermissive';value=$oldRun} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backupDirectory 'registry-startup.json') -Encoding UTF8
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Trigger @($logon,$recovery) -Settings $settings -Description 'Existing Codex Permissive backend; starts at sign-in and automatically recovers exits.' -Force | Out-Null
if ($oldRun) { Remove-ItemProperty -LiteralPath $runKey -Name 'CodexPermissive' }
Start-ScheduledTask -TaskName $taskName
@{taskName=$taskName;homeDirectory=$HomeDirectory;backupDirectory=$backupDirectory} | ConvertTo-Json -Compress
