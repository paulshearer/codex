param(
    [string]$Registry,
    [string]$ProfileDirectory = (Join-Path $env:LOCALAPPDATA 'CodexCustomModels'),
    [string]$PackageDirectory = (Split-Path $PSScriptRoot)
)
. "$PSScriptRoot/validation-common.ps1"
$PackageDirectory = (Resolve-Path -LiteralPath $PackageDirectory).Path
if (!$Registry) { $Registry = Join-Path $PackageDirectory 'registry.json' }
$Registry = (Resolve-Path -LiteralPath $Registry).Path
$manifestHash = Assert-PackageHashes $PackageDirectory
$validation = Get-Content -LiteralPath (Join-Path $PackageDirectory 'desktop-validation.json') -Raw | ConvertFrom-Json
if ($validation.schema -ne 'codex-custom-desktop-validation/v1' -or $validation.packageManifestSha256 -ne $manifestHash) { throw 'Run windows/revalidate.ps1 before launching this package.' }
$executable = Assert-DesktopFingerprint $validation.desktop
$ProfileDirectory = [IO.Path]::GetFullPath($ProfileDirectory)
$defaultHome = [IO.Path]::GetFullPath((Join-Path $env:USERPROFILE '.codex'))
if ($ProfileDirectory -eq $defaultHome -or $ProfileDirectory.StartsWith($defaultHome + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Choose a separate profile directory outside the normal Codex home.' }
New-Item -ItemType Directory -Force -Path $ProfileDirectory | Out-Null
$saved = @{}
$settings = @{
    CODEX_CLI_PATH = (Join-Path $PackageDirectory 'codex-custom.exe')
    CUSTOM_CODEX_NATIVE = (Join-Path $PackageDirectory 'native/codex.exe')
    CUSTOM_CODEX_REGISTRY = $Registry
    CUSTOM_CODEX_HOME = (Join-Path $ProfileDirectory 'codex-home')
    CODEX_HOME = (Join-Path $ProfileDirectory 'codex-home')
    CODEX_ELECTRON_USER_DATA_PATH = (Join-Path $ProfileDirectory 'desktop-data')
}
try {
    foreach ($entry in $settings.GetEnumerator()) { $saved[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key, 'Process'); [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process') }
    $argument = '--user-data-dir="' + (Join-Path $ProfileDirectory 'chromium-data') + '"'
    $process = Start-Process -FilePath $executable -ArgumentList $argument -PassThru -WindowStyle Hidden
    Write-Host "Launched isolated Codex desktop profile (PID $($process.Id)): $ProfileDirectory"
} finally {
    foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
}
