param([string]$CacheDirectory = 'C:\codex-build\bootstrap')
. "$PSScriptRoot/common.ps1"
Assert-FreeBuildSpace $CacheDirectory 15
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$installed = if (Test-Path -LiteralPath $vswhere) { & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath }
if (!$installed) {
    & winget.exe install --exact --id Microsoft.VisualStudio.2022.BuildTools --version 17.14.41 --source winget --accept-package-agreements --accept-source-agreements --silent --override '--quiet --wait --norestart --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Component.VC.CMake.Project --add Microsoft.VisualStudio.Component.Windows11SDK.26100'
    if ($LASTEXITCODE -notin @(0, 3010)) { throw "Visual Studio installation failed: $LASTEXITCODE" }
}
$rustup = Join-Path $env:USERPROFILE '.cargo/bin/rustup.exe'
if (!(Test-Path -LiteralPath $rustup)) {
    $installer = Get-VerifiedDownload 'https://static.rust-lang.org/rustup/archive/1.29.0/x86_64-pc-windows-msvc/rustup-init.exe' (Join-Path $CacheDirectory 'rustup-init-1.29.0.exe') '86478e53f769379d7f0ebfa7c9aa97cb76ca92233f79aa2cc0dbee2efaac73c7'
    & $installer -y --no-modify-path --profile minimal --default-toolchain 1.95.0-x86_64-pc-windows-msvc
    if ($LASTEXITCODE) { throw "rustup installation failed: $LASTEXITCODE" }
}
$env:PATH = (Join-Path $env:USERPROFILE '.cargo/bin') + ';' + $env:PATH
& $rustup toolchain install 1.95.0-x86_64-pc-windows-msvc --profile minimal --component rustfmt,clippy,rust-src --no-self-update
if ($LASTEXITCODE) { throw 'Rust toolchain installation failed.' }
Enter-MsvcEnvironment
$versions = @{ 'just' = '1.58.0'; 'dotslash' = '0.5.7'; 'cargo-nextest' = '0.9.146' }
foreach ($tool in @('just', 'dotslash', 'cargo-nextest')) {
    $command = if ($tool -eq 'cargo-nextest') { 'cargo-nextest' } else { $tool }
    if (!(Get-Command $command -ErrorAction SilentlyContinue)) {
        & cargo.exe +1.95.0 install --locked $tool --version $versions[$tool]
        if ($LASTEXITCODE) { throw "Installation failed: $tool" }
    }
}
& rustc.exe +1.95.0 --version
