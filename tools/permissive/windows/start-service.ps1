param([string]$PackageDirectory = (Split-Path $PSScriptRoot -Parent))
$ErrorActionPreference = 'Stop'
$homeDirectory = if ($env:PERMISSIVE_HOME) { [IO.Path]::GetFullPath($env:PERMISSIVE_HOME) } else { Join-Path $env:LOCALAPPDATA 'CodexPermissive' }
New-Item -ItemType Directory -Force -Path $homeDirectory | Out-Null
$env:PERMISSIVE_PACKAGE = [IO.Path]::GetFullPath($PackageDirectory)
$env:PERMISSIVE_HOME = $homeDirectory
$env:PATH = ((@('native', 'node', 'python', 'powershell') | ForEach-Object { Join-Path $PackageDirectory $_ }) -join ';') + ';' + $env:PATH
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PackageDirectory 'chromium'
# The Rust launcher gives the detached service null input/output/error handles.
# Unhandled Python errors cannot become prompt-bearing service diagnostic files.
$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = Join-Path $PackageDirectory 'CodexPermissive.exe'
$start.Arguments = '--start-service'
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$launcher = [Diagnostics.Process]::new()
$launcher.StartInfo = $start
if (!$launcher.Start()) { throw 'Authenticated local service startup failed.' }
if (!$launcher.WaitForExit(60000)) { $launcher.Kill(); throw 'Authenticated local service startup timed out.' }
if ($launcher.ExitCode -ne 0) { throw 'Authenticated local service startup failed.' }
$launcher.Dispose()
