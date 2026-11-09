param([string]$PackageDirectory = (Split-Path $PSScriptRoot -Parent),
      [string]$InstallDirectory = (Join-Path $env:LOCALAPPDATA 'Programs/CodexPermissive/1.0.0-tauri'),
      [string]$BindingPath)
$ErrorActionPreference = 'Stop'
$PackageDirectory = (Resolve-Path -LiteralPath $PackageDirectory).Path
if (!(Test-Path -LiteralPath (Join-Path $PackageDirectory 'manifest.json'))) { throw 'A built standalone package is required.' }
& (Join-Path $PSScriptRoot 'verify.ps1') -PackageDirectory $PackageDirectory
. (Join-Path $PSScriptRoot 'sandbox/common.ps1')
Assert-SandboxAdministrator
if (!$BindingPath) { $BindingPath = Join-Path $PackageDirectory 'deployment.json' }
$binding = Read-SandboxBinding $BindingPath
if ($binding.status -ne 'ready') { throw 'Complete the sandbox provisioning/import before installing its controller.' }
# Use the canonical binding so the scheduled task and desktop share one VM.
$BindingPath = Join-Path $binding.stateDirectory 'deployment.json'
$binding = Read-SandboxBinding $BindingPath
$InstallDirectory = [IO.Path]::GetFullPath($InstallDirectory)
if (Test-Path -LiteralPath (Join-Path $InstallDirectory 'electron')) { throw 'Choose a fresh installation directory for the Rust desktop.' }
if ($PackageDirectory -ne $InstallDirectory) {
    New-Item -ItemType Directory -Force -Path $InstallDirectory | Out-Null
    & robocopy.exe $PackageDirectory $InstallDirectory /E /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -gt 7) { throw 'Standalone installation copy failed.' }
    $PackageDirectory = $InstallDirectory
    & (Join-Path $PSScriptRoot 'verify.ps1') -PackageDirectory $PackageDirectory
}
$binding.packagePath = $PackageDirectory
Write-SandboxJson $BindingPath $binding
Install-SandboxControllerBinding $BindingPath $PackageDirectory
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Codex Permissive.lnk'))
$shortcut.TargetPath = Join-Path $PackageDirectory 'CodexPermissive.exe'
$shortcut.WorkingDirectory = $PackageDirectory
$shortcut.Save()
# Existing taskbar pins otherwise keep launching an older version directory.
$pinnedDirectory = Join-Path $env:APPDATA 'Microsoft/Internet Explorer/Quick Launch/User Pinned/TaskBar'
if (Test-Path -LiteralPath $pinnedDirectory) {
    foreach ($pin in Get-ChildItem -LiteralPath $pinnedDirectory -Filter '*.lnk' -File) {
        $pinnedShortcut = $shell.CreateShortcut($pin.FullName)
        if ($pinnedShortcut.TargetPath -match '[\\/]Programs[\\/]CodexPermissive[\\/].*[\\/]CodexPermissive\.exe$') {
            $pinnedShortcut.TargetPath = Join-Path $PackageDirectory 'CodexPermissive.exe'
            $pinnedShortcut.WorkingDirectory = $PackageDirectory
            $pinnedShortcut.IconLocation = $pinnedShortcut.TargetPath + ',0'
            $pinnedShortcut.Save()
        }
    }
}
$startup = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + (Join-Path $PackageDirectory 'windows/start-service.ps1') + '"'
New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'CodexPermissive' -Value $startup -PropertyType String -Force | Out-Null
& tailscale.exe serve --bg --https=9443 http://127.0.0.1:8789
if ($LASTEXITCODE) { throw 'Tailscale API exposure failed.' }
& (Join-Path $PSScriptRoot 'start-service.ps1') -PackageDirectory $PackageDirectory
Write-Host 'Installed Codex Permissive shortcut, user-logon service, and authenticated tailnet API.'
