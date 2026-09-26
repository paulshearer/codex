param([string]$BuildDirectory = 'C:\codex-build\permissive', [string]$Python = 'python.exe')
. "$PSScriptRoot/common.ps1"
Assert-FreeBuildSpace $BuildDirectory 15
Enter-MsvcEnvironment
$env:CARGO_TARGET_DIR = $BuildDirectory
$env:LIBSQLITE3_FLAGS = 'SQLITE_DISABLE_INTRINSIC'
$env:STABLE_GIT_COMMIT = (& git.exe -C $RepoRoot rev-parse HEAD).Trim()
$env:CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER = Join-Path ((& rustc.exe +1.95.0 --print sysroot).Trim()) 'lib/rustlib/x86_64-pc-windows-msvc/bin/rust-lld.exe'

# Reproduce .github/actions/setup-rusty-v8, including the source-pinned manifest.
$version = (& $Python (Join-Path $RepoRoot '.github/scripts/rusty_v8_bazel.py') resolved-v8-crate-version).Trim()
if ($LASTEXITCODE) { throw 'Cannot resolve the source-pinned V8 version.' }
$profile = 'ptrcomp_sandbox_release'
$names = @("rusty_v8_${profile}_${Target}.lib.gz", "src_binding_${profile}_${Target}.rs")
$checksumName = "rusty_v8_${profile}_${Target}.sha256"
$trustedPath = Join-Path $RepoRoot ('third_party/v8/rusty_v8_' + $version.Replace('.', '_') + '_release_manifests.sha256')
$line = @(Get-Content -LiteralPath $trustedPath | Where-Object { $_.EndsWith('  ' + $checksumName) })
if ($line.Count -ne 1) { throw 'The source does not pin exactly one Windows V8 manifest.' }
$v8Directory = Join-Path $BuildDirectory 'downloads/v8'
$baseUrl = "https://github.com/openai/codex/releases/download/rusty-v8-v$version"
$manifest = Get-VerifiedDownload "$baseUrl/$checksumName" (Join-Path $v8Directory $checksumName) $line[0].Split(' ')[0]
$checksums = @(Get-Content -LiteralPath $manifest)
if ($checksums.Count -ne 2) { throw 'Unexpected V8 manifest shape.' }
foreach ($name in $names) {
    $entry = @($checksums | Where-Object { $_.EndsWith('  ' + $name) })
    if ($entry.Count -ne 1) { throw "V8 manifest does not pin $name" }
    Get-VerifiedDownload "$baseUrl/$name" (Join-Path $v8Directory $name) $entry[0].Split(' ')[0] | Out-Null
}
$env:RUSTY_V8_ARCHIVE = Join-Path $v8Directory $names[0]
$env:RUSTY_V8_SRC_BINDING_PATH = Join-Path $v8Directory $names[1]
$sourceDirectory = Join-Path $BuildDirectory 'source'
New-Item -ItemType Directory -Force -Path $sourceDirectory | Out-Null
# The upstream release tag changes workspace versions without changing local
# lockfile versions. Stage sources before normalizing; the checkout stays exact.
& robocopy.exe $RepoRoot $sourceDirectory /E /NFL /NDL /NJH /NJS /NP /XD .git target __pycache__ .venv out dist .cache (Join-Path $RepoRoot 'tools/custom-models')
if ($LASTEXITCODE -gt 7) { throw "Source staging failed: $LASTEXITCODE" }
& $Python (Join-Path $PSScriptRoot 'normalize-lock.py') (Join-Path $sourceDirectory 'codex-rs')
if ($LASTEXITCODE) { throw 'Release lock normalization failed.' }
$arguments = @('+1.95.0', 'build', '--locked', '--target', $Target, '--release', '--timings')
foreach ($binary in $Binaries) { $arguments += @('--bin', $binary) }
Push-Location (Join-Path $sourceDirectory 'codex-rs')
try {
    & cargo.exe @arguments
    if ($LASTEXITCODE) { throw "Native build failed: $LASTEXITCODE" }
} finally { Pop-Location }
$binaryHashes = @{}
foreach ($binary in $Binaries) {
    $binaryHashes[$binary + '.exe'] = (Get-FileHash -LiteralPath (Join-Path $BuildDirectory "$Target/release/$binary.exe") -Algorithm SHA256).Hash.ToLowerInvariant()
}
[ordered]@{
    schema = 'codex-custom-native-build/v1'
    sourceCommit = $env:STABLE_GIT_COMMIT
    upstreamTag = 'rust-v0.157.1'
    target = $Target
    rustToolchain = '1.95.0-x86_64-pc-windows-msvc'
    buildLockSha256 = (Get-FileHash -LiteralPath (Join-Path $sourceDirectory 'codex-rs/Cargo.lock') -Algorithm SHA256).Hash.ToLowerInvariant()
    v8Version = $version
    v8ManifestSha256 = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash.ToLowerInvariant()
    binaries = $binaryHashes
    completedAt = [DateTime]::UtcNow.ToString('o')
} | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $BuildDirectory 'build.json') -Encoding utf8
Assert-FreeBuildSpace $BuildDirectory
