#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)][string]$BindingPath, [switch]$StartTask)
$ErrorActionPreference = 'Stop'
$binding = Get-Content -LiteralPath $BindingPath -Raw | ConvertFrom-Json
if ($binding.role -ne 'controller') { throw 'An explicit controller deployment is required.' }
$sandboxId = ([Guid]$binding.sandboxId).ToString()
$taskName = 'CodexPermissiveController-' + $sandboxId
if ($StartTask) {
    # Starting this previously registered fixed task does not elevate the UI.
    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
    return
}
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SandboxAdministrator
$binding = Read-SandboxBinding $BindingPath
$package = Get-SandboxPackage $binding.packagePath
$env:PERMISSIVE_PACKAGE = $package
$env:PERMISSIVE_HOME = [IO.Path]::GetFullPath($binding.controllerHome)
$env:PERMISSIVE_DEPLOYMENT = [IO.Path]::GetFullPath($BindingPath)
$env:PERMISSIVE_ROLE = 'controller'
$env:PATH = ((@('native','node','python','powershell') | ForEach-Object {Join-Path $package $_}) -join ';') + ';' + $env:PATH
Set-Location -LiteralPath $package
$port = if ($binding.PSObject.Properties['controllerPort']) {[int]$binding.controllerPort} else {8789}
if ($port -lt 1024 -or $port -gt 65535) { throw 'Invalid controller loopback port.' }
& (Join-Path $package 'python\python.exe') -B -X utf8 -m permissive.auth --home $env:PERMISSIVE_HOME init-local --ensure *> $null
if ($LASTEXITCODE) { throw 'Controller-local API identity initialization failed.' }
# Service locks prevent duplicate controllers. Prompt-bearing output is discarded.
& (Join-Path $package 'python\python.exe') -B -X utf8 -m permissive --home $env:PERMISSIVE_HOME serve --role controller --deployment $env:PERMISSIVE_DEPLOYMENT --port $port *> $null
exit $LASTEXITCODE
