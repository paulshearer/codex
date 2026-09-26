Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$Target = 'x86_64-pc-windows-msvc'
$Binaries = @('codex', 'codex-app-server', 'codex-code-mode-host', 'codex-responses-api-proxy', 'codex-windows-sandbox-setup', 'codex-windows-sandbox-service', 'codex-command-runner')

function Get-VerifiedDownload([string]$Url, [string]$Destination, [string]$Sha256) {
    if (!(Test-Path -LiteralPath $Destination)) {
        New-Item -ItemType Directory -Force -Path (Split-Path $Destination) | Out-Null
        Invoke-WebRequest -Uri $Url -OutFile $Destination
    }
    if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -ine $Sha256) {
        throw "Checksum mismatch: $Destination. Remove that file and retry."
    }
    return $Destination
}

function Enter-MsvcEnvironment {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
    if (!(Test-Path -LiteralPath $vswhere)) { throw 'Run bootstrap.ps1 first: Visual Studio Build Tools are missing.' }
    $installation = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (!$installation) { throw 'The x64 Visual Studio C++ tools are missing.' }
    $devcmd = Join-Path $installation 'Common7/Tools/VsDevCmd.bat'
    # This fixed command initializes the documented Microsoft developer environment.
    $lines = & $env:ComSpec /d /c ('"{0}" -no_logo -arch=x64 -host_arch=x64 >nul && set' -f $devcmd)
    if ($LASTEXITCODE) { throw 'VsDevCmd failed.' }
    foreach ($line in $lines) {
        if ($line -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process') }
    }
    $env:PATH = (Join-Path $env:USERPROFILE '.cargo/bin') + ';' + $env:PATH
}

function Assert-FreeBuildSpace([string]$Directory, [int]$MinimumGiB = 8) {
    $drive = Get-PSDrive -Name ([IO.Path]::GetPathRoot($Directory).Substring(0, 1))
    $free = [Math]::Round($drive.Free / 1GB, 1)
    Write-Host "Build volume free space: $free GiB"
    if ($free -lt $MinimumGiB) { throw "At least $MinimumGiB GiB free space is required before starting another build step." }
}
