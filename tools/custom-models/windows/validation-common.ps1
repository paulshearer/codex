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

function Resolve-DesktopApplication($Package, $Manifest) {
    $applications = @($Manifest.Package.Applications.Application | Where-Object { $_.Id -ceq 'App' })
    if ($applications.Count -ne 1) { throw 'The installed Codex manifest must contain exactly one Application with Id=App.' }
    $main = $applications[0]
    $relative = [string]$main.Executable
    if ([string]::IsNullOrWhiteSpace($relative) -or [IO.Path]::IsPathRooted($relative)) { throw 'The Codex manifest executable must be a relative package path.' }
    $root = [IO.Path]::GetFullPath($Package.InstallLocation).TrimEnd('\')
    $executable = [IO.Path]::GetFullPath((Join-Path $root $relative))
    if (!$executable.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'The Codex manifest executable escapes its installed package.' }
    if ([IO.Path]::GetExtension($executable) -ine '.exe' -or !(Test-Path -LiteralPath $executable -PathType Leaf)) { throw 'The Codex manifest main executable was not found.' }
    return [ordered]@{
        packageFullName = $Package.PackageFullName
        packageFamilyName = $Package.PackageFamilyName
        applicationId = [string]$main.Id
        entryPoint = [string]$main.EntryPoint
        manifestExecutable = $relative.Replace('\', '/')
        application = (Split-Path $executable)
        executable = $executable
    }
}

function Get-DesktopFingerprint {
    $package = Get-AppxPackage OpenAI.Codex | Select-Object -First 1
    if (!$package) { throw 'The installed OpenAI Codex desktop package was not found.' }
    $manifest = $package | Get-AppxPackageManifest
    $identity = Resolve-DesktopApplication $package $manifest
    $application = $identity.application
    $names = @([IO.Path]::GetFileName($identity.executable), 'resources/app.asar', 'resources/codex.exe', 'resources/codex-code-mode-host.exe', 'resources/codex-command-runner.exe', 'resources/codex-windows-sandbox-setup.exe', 'resources/codex-windows-sandbox-service.exe', 'resources/rg.exe')
    $hashes = [ordered]@{}
    foreach ($name in $names) { $hashes[$name] = (Get-FileHash -LiteralPath (Join-Path $application $name) -Algorithm SHA256).Hash.ToLowerInvariant() }
    $identity['version'] = $package.Version.ToString()
    $identity['files'] = $hashes
    return $identity
}

function Assert-DesktopFingerprint($Expected) {
    $current = Get-DesktopFingerprint
    foreach ($name in @('version', 'packageFullName', 'packageFamilyName', 'applicationId', 'entryPoint', 'manifestExecutable', 'application', 'executable')) {
        $entry = $Expected.PSObject.Properties[$name]
        if (!$entry -or $current[$name] -ne $entry.Value) { throw 'Codex desktop changed. Run windows/revalidate.ps1 before launching this profile.' }
    }
    $files = $Expected.PSObject.Properties['files']
    if (!$files -or @($files.Value.PSObject.Properties).Count -ne $current.files.Count) { throw 'Codex desktop fingerprint is incomplete. Run windows/revalidate.ps1.' }
    foreach ($name in $current.files.Keys) {
        $entry = $files.Value.PSObject.Properties[$name]
        if (!$entry -or $current.files[$name] -ne $entry.Value) { throw "Codex desktop changed: $name. Run windows/revalidate.ps1." }
    }
    return $current.executable
}
