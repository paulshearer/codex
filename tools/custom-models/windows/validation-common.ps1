Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-PackageHashes([string]$PackageDirectory) {
    $manifest = Get-Content -LiteralPath (Join-Path $PackageDirectory 'manifest.json') -Raw | ConvertFrom-Json
    if ($manifest.schema -ne 'codex-custom-package/v1') { throw 'Unsupported package manifest.' }
    foreach ($entry in $manifest.files.PSObject.Properties) {
        $path = [IO.Path]::GetFullPath((Join-Path $PackageDirectory $entry.Name))
        if (!$path.StartsWith([IO.Path]::GetFullPath($PackageDirectory).TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Manifest path escapes the package.' }
        if (!(Test-Path -LiteralPath $path) -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ine $entry.Value) { throw "Package file changed: $($entry.Name). Rebuild the package." }
    }
    return (Get-FileHash -LiteralPath (Join-Path $PackageDirectory 'manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-DesktopFingerprint {
    $package = Get-AppxPackage OpenAI.Codex | Select-Object -First 1
    if (!$package) { throw 'The installed OpenAI Codex desktop package was not found.' }
    $application = Join-Path $package.InstallLocation 'app'
    $names = @('Codex.exe', 'resources/app.asar', 'resources/codex.exe', 'resources/codex-code-mode-host.exe', 'resources/codex-command-runner.exe', 'resources/codex-windows-sandbox-setup.exe', 'resources/codex-windows-sandbox-service.exe', 'resources/rg.exe')
    $hashes = [ordered]@{}
    foreach ($name in $names) { $hashes[$name] = (Get-FileHash -LiteralPath (Join-Path $application $name) -Algorithm SHA256).Hash.ToLowerInvariant() }
    return [ordered]@{ version = $package.Version.ToString(); application = $application; files = $hashes }
}

function Assert-DesktopFingerprint($Expected) {
    $current = Get-DesktopFingerprint
    if ($current.version -ne $Expected.version -or $current.application -ne $Expected.application) { throw 'Codex desktop changed. Run windows/revalidate.ps1 before launching this profile.' }
    foreach ($entry in $Expected.files.PSObject.Properties) {
        if ($current.files[$entry.Name] -ne $entry.Value) { throw "Codex desktop changed: $($entry.Name). Run windows/revalidate.ps1." }
    }
    return $current.application
}
