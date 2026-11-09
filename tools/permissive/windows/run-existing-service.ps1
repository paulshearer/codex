# Existing host-runtime installations only. VM controllers use Start-Controller.ps1.
# Keep the child attached so Task Scheduler observes exits and can restart it.
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
if (!(Test-Path -LiteralPath (Join-Path $HomeDirectory 'tasks.sqlite') -PathType Leaf)) {
    throw 'Select the existing application history directory.'
}
foreach ($path in @($PackageDirectory,$HomeDirectory)) {
    if ($path.Contains('"')) { throw 'An application path contains an unsupported quote.' }
}
$env:PERMISSIVE_PACKAGE = $PackageDirectory
$env:PERMISSIVE_HOME = $HomeDirectory
$env:PYTHONPATH = Join-Path $PackageDirectory 'lib'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PackageDirectory 'chromium'
$env:PATH = ((@('native','node','python','powershell') | ForEach-Object { Join-Path $PackageDirectory $_ }) -join ';') + ';' + $env:PATH
$env:RUST_LOG = 'off'
$start = New-Object Diagnostics.ProcessStartInfo
$start.FileName = Join-Path $PackageDirectory 'python/python.exe'
$start.WorkingDirectory = $PackageDirectory
$start.Arguments = '-B -X utf8 -m permissive --home "' + $HomeDirectory + '" serve --native "' + (Join-Path $PackageDirectory 'native/codex-app-server.exe') + '" --node "' + (Join-Path $PackageDirectory 'node/node.exe') + '" --browser-worker "' + (Join-Path $PackageDirectory 'browser/worker.mjs') + '"'
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardInput = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$child = New-Object Diagnostics.Process
$child.StartInfo = $start
if (!$child.Start()) { throw 'The existing application service could not start.' }
$child.StandardInput.Close()
$output = $child.StandardOutput.BaseStream.CopyToAsync([IO.Stream]::Null)
$errors = $child.StandardError.BaseStream.CopyToAsync([IO.Stream]::Null)
$child.WaitForExit()
$null = $output.GetAwaiter().GetResult()
$null = $errors.GetAwaiter().GetResult()
$code = $child.ExitCode
$child.Dispose()
exit $code
