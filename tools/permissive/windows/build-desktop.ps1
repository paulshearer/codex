param([string]$BuildDirectory = 'C:\codex-build\permissive-desktop', [switch]$Debug, [switch]$E2E, [switch]$Run)
. "$PSScriptRoot/../../custom-models/windows/common.ps1"
if ($E2E -and !$Debug) { throw 'UI test hooks require a debug build.' }
Enter-MsvcEnvironment
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $root
try {
    & npm.cmd run build-ui
    if ($LASTEXITCODE) { throw 'Desktop assets failed to build.' }
    $env:CARGO_TARGET_DIR = $BuildDirectory
    $metadataText = & cargo.exe +1.95.0 metadata --locked --format-version 1 --filter-platform x86_64-pc-windows-msvc --manifest-path (Join-Path $root 'desktop-rs/Cargo.toml')
    if ($LASTEXITCODE) { throw 'Cannot verify desktop dependency sources.' }
    $metadata = $metadataText | ConvertFrom-Json
    $webview = @($metadata.packages | Where-Object { $_.name -eq 'wry' })
    $patchedManifest = Join-Path $root 'desktop-rs/vendor/wry/Cargo.toml'
    if ($webview.Count -ne 1 -or [IO.Path]::GetFullPath($webview[0].manifest_path) -ne [IO.Path]::GetFullPath($patchedManifest)) { throw 'The desktop must use its local WebView crash-upload privacy patch.' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $root 'desktop-rs/vendor/wry/src/webview2/mod.rs')) -notmatch 'set_is_custom_crash_reporting_enabled\(true\)') { throw 'The WebView crash-upload privacy patch is missing.' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $root 'desktop-rs/vendor/wry/src/webview2/mod.rs')) -notmatch 'SetIsReputationCheckingRequired\(false\)') { throw 'The WebView reputation-upload privacy patch is missing.' }
    if ((Get-Content -Raw -LiteralPath (Join-Path $root 'desktop-rs/vendor/wry/src/webview2/mod.rs')) -notmatch 'SetIsPasswordAutosaveEnabled\(false\)') { throw 'The WebView password-saving protection is missing.' }
    $arguments = @('+1.95.0', 'build', '--locked', '--manifest-path', (Join-Path $root 'desktop-rs/Cargo.toml'))
    if (!$Debug) { $arguments += '--release' }
    if ($E2E) { $arguments += @('--features', 'e2e') }
    & cargo.exe @arguments
    if ($LASTEXITCODE) { throw 'Rust desktop build failed.' }
    $profile = if ($Debug) { 'debug' } else { 'release' }
    $binary = Join-Path $BuildDirectory "$profile/CodexPermissive.exe"
    if (!$Debug) {
        $sourceInputs = @{}
        $sources = @(Get-ChildItem -LiteralPath (Join-Path $root 'desktop-rs'),(Join-Path $root 'desktop') -File -Recurse | Where-Object { $_.FullName -notmatch '[\\/](gen|target|dist)[\\/]' -and $_.Extension -in @('.rs','.toml','.json','.lock','.jsx','.mjs','.css','.html','.png','.ico') })
        $sources += Get-Item -LiteralPath (Join-Path $root 'package.json'),(Join-Path $root 'package-lock.json')
        foreach ($source in $sources) { $sourceInputs[$source.FullName.Substring($root.Length + 1).Replace('\','/')] = (Get-FileHash -LiteralPath $source.FullName).Hash.ToLowerInvariant() }
        $licenseDirectory = Join-Path $BuildDirectory 'licenses'
        New-Item -ItemType Directory -Force -Path $licenseDirectory | Out-Null
        foreach ($dependency in $metadata.packages) {
            $directory = Split-Path $dependency.manifest_path -Parent
            $licenses = @(Get-ChildItem -LiteralPath $directory -File | Where-Object { $_.Name -match '^(LICENSE|LICENCE|COPYING|NOTICE)' })
            foreach ($license in $licenses) { Copy-Item -LiteralPath $license.FullName -Destination (Join-Path $licenseDirectory ($dependency.name + '-' + $dependency.version + '-' + $license.Name)) -Force }
        }
        # QR rendering is bundled into the desktop executable by esbuild.
        foreach ($module in @(@('qrcode','license'), @('dijkstrajs','LICENSE.md'))) {
            Copy-Item -LiteralPath (Join-Path $root ('node_modules/' + $module[0] + '/' + $module[1])) -Destination (Join-Path $licenseDirectory ('npm-' + $module[0] + '-LICENSE')) -Force
        }
        [ordered]@{ schema='codex-permissive-desktop-build/v1'; framework='tauri'; version='1.0.0'; privacyPolicy='no-content-diagnostics-v1'; webviewPrivacyPolicy='disable-microsoft-crash-upload-v1'; binarySha256=(Get-FileHash -LiteralPath $binary).Hash.ToLowerInvariant(); lockSha256=(Get-FileHash -LiteralPath (Join-Path $root 'desktop-rs/Cargo.lock')).Hash.ToLowerInvariant(); sourceInputs=$sourceInputs; completedAt=[DateTime]::UtcNow.ToString('o') } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $BuildDirectory 'desktop-build.json') -Encoding utf8
    }
    if ($Run) {
        if (!$env:PERMISSIVE_PACKAGE) {
            $runtimePackage = Join-Path (Split-Path ([IO.Path]::GetFullPath($BuildDirectory)) -Parent) 'CodexPermissive-Tauri-1.0.0'
            if (!(Test-Path -LiteralPath (Join-Path $runtimePackage 'manifest.json'))) { throw 'Build the standalone package first, or set PERMISSIVE_PACKAGE to a verified runtime package.' }
            $env:PERMISSIVE_PACKAGE = $runtimePackage
        }
        Start-Process -FilePath $binary -WorkingDirectory $root
    }
} finally { Pop-Location }
