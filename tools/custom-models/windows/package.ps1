param(
    [string]$BuildDirectory = 'C:\codex-build\permissive',
    [string]$PackageDirectory = (Join-Path $PSScriptRoot '../out/codex-custom-0.157.1-windows-x64'),
    [string]$CacheDirectory = 'C:\codex-build\bootstrap'
)
. "$PSScriptRoot/common.ps1"
$PackageDirectory = [IO.Path]::GetFullPath($PackageDirectory)
Assert-FreeBuildSpace $PackageDirectory
if (Test-Path -LiteralPath $PackageDirectory) { throw 'Choose a new package directory; existing packages are preserved.' }
$nativeBuild = Get-Content -LiteralPath (Join-Path $BuildDirectory 'build.json') -Raw | ConvertFrom-Json
if ($nativeBuild.schema -ne 'codex-custom-native-build/v1' -or $nativeBuild.target -ne $Target) { throw 'Unsupported native build metadata; rebuild with windows/build.ps1.' }
$pythonZip = Get-VerifiedDownload 'https://www.python.org/ftp/python/3.13.7/python-3.13.7-embed-amd64.zip' (Join-Path $CacheDirectory 'python-3.13.7-embed-amd64.zip') 'f6cca216a359be84797cabb54149ce5e062afb16cc7567eb7fc51cacb2d86b65'
New-Item -ItemType Directory -Force -Path $PackageDirectory,(Join-Path $PackageDirectory 'native'),(Join-Path $PackageDirectory 'lib'),(Join-Path $PackageDirectory 'windows') | Out-Null
foreach ($binary in $Binaries) {
    $nativePath = Join-Path $BuildDirectory "$Target/release/$binary.exe"
    if ((Get-FileHash -LiteralPath $nativePath -Algorithm SHA256).Hash -ine $nativeBuild.binaries.PSObject.Properties[$binary + '.exe'].Value) { throw "Native build output changed: $binary. Rebuild before packaging." }
    Copy-Item -LiteralPath $nativePath -Destination (Join-Path $PackageDirectory 'native')
}
# The native install context falls back to PATH outside the official package
# layout. Bundle the desktop's signed ripgrep; the source-pinned release archive
# supplies the original licenses for the same version.
$desktop = Get-AppxPackage OpenAI.Codex | Select-Object -First 1
if (!$desktop) { throw 'The official Codex desktop is required to source ripgrep.' }
$desktopRg = Join-Path $desktop.InstallLocation 'app/resources/rg.exe'
$rgSpec = ((Get-Content -LiteralPath (Join-Path $RepoRoot 'scripts/codex_package/rg') -Raw) -replace '^#![^\n]*\n', '' | ConvertFrom-Json).platforms.'windows-x86_64'
$rgUrl = $rgSpec.providers[0].url
$rgArchive = Get-VerifiedDownload $rgUrl (Join-Path $CacheDirectory ([IO.Path]::GetFileName($rgUrl))) $rgSpec.digest
$rgDirectory = Join-Path $CacheDirectory 'ripgrep-source-pinned'
Expand-Archive -LiteralPath $rgArchive -DestinationPath $rgDirectory -Force
$referenceRg = Join-Path $rgDirectory $rgSpec.path
$rgSignature = Get-AuthenticodeSignature -LiteralPath $desktopRg
if ($rgSignature.Status -ne 'Valid' -or $rgSignature.SignerCertificate.Subject -notmatch 'CN="?OpenAI OpCo, LLC') { throw 'The desktop ripgrep signature is not a valid OpenAI signature.' }
$rgOutput = @(& $desktopRg --version)
$rgVersion = $rgOutput[0]
$referenceVersion = @(& $referenceRg --version)[0]
if ($rgVersion -notmatch '^ripgrep ([0-9.]+)') { throw 'Cannot parse desktop ripgrep version.' }
$rgVersion = $Matches[1]
if ($referenceVersion -notmatch '^ripgrep ([0-9.]+)' -or $Matches[1] -ne $rgVersion) { throw 'Desktop ripgrep version differs from the source-pinned release; inspect the upstream package manifest before packaging.' }
Copy-Item -LiteralPath $desktopRg -Destination (Join-Path $PackageDirectory 'native/rg.exe')
$rgLicenses = Join-Path $PackageDirectory 'licenses/ripgrep'
New-Item -ItemType Directory -Force -Path $rgLicenses | Out-Null
foreach ($name in @('COPYING', 'LICENSE-MIT', 'UNLICENSE')) {
    Copy-Item -LiteralPath (Join-Path (Split-Path $referenceRg) $name) -Destination $rgLicenses
}
if (-not ($rgOutput -match '^PCRE2 10\.48 is available')) { throw 'The bundled ripgrep PCRE2 version changed; update and verify its license sources before packaging.' }
$pcre2License = Get-VerifiedDownload 'https://raw.githubusercontent.com/PCRE2Project/pcre2/pcre2-10.48/LICENCE.md' (Join-Path $CacheDirectory 'pcre2-10.48-LICENCE.md') '197d8a73ffee0d6b09adba2f9c677b5f5aede24edf89258a68e48248d010d811'
$sljitLicense = Get-VerifiedDownload 'https://raw.githubusercontent.com/zherczeg/sljit/de0259c7aaf36aa40cba8014f3fad3edde9307f9/LICENSE' (Join-Path $CacheDirectory 'sljit-pcre2-10.48-LICENSE') '5f216505c0f6ea3273caec89e766eef93cdeb7bbb0c429f9360116d7c938feeb'
Copy-Item -LiteralPath $pcre2License -Destination (Join-Path $rgLicenses 'PCRE2-LICENCE.md')
Copy-Item -LiteralPath $sljitLicense -Destination (Join-Path $rgLicenses 'SLJIT-LICENSE')
Expand-Archive -LiteralPath $pythonZip -DestinationPath (Join-Path $PackageDirectory 'python')
[IO.File]::WriteAllText((Join-Path $PackageDirectory 'python/python313._pth'), "python313.zip`n.`n../lib`n", [Text.UTF8Encoding]::new($false))
$moduleDirectory = Join-Path $PackageDirectory 'lib/custom_models'
& robocopy.exe (Join-Path $RepoRoot 'tools/custom-models/custom_models') $moduleDirectory /E /NFL /NDL /NJH /NJS /NP /XF '*.pyc' /XD __pycache__
if ($LASTEXITCODE -gt 7) { throw "Companion staging failed: $LASTEXITCODE" }
Copy-Item -LiteralPath (Join-Path $RepoRoot 'tools/custom-models/assets') -Destination (Join-Path $PackageDirectory 'lib') -Recurse
New-Item -ItemType Directory -Force -Path (Join-Path $PackageDirectory 'tests') | Out-Null
Copy-Item -Path (Join-Path $RepoRoot 'tools/custom-models/test_*.py') -Destination (Join-Path $PackageDirectory 'tests')
Copy-Item -LiteralPath (Join-Path $RepoRoot 'tools/custom-models/registry.example.json') -Destination $PackageDirectory
foreach ($name in @('README.md', 'REFUSAL-AUDIT.md', 'VERIFICATION.md', 'registry.deepseek.example.json', 'native-acceptance.py', 'cli-acceptance.py', 'advanced-acceptance.py')) {
    $path = Join-Path $RepoRoot ('tools/custom-models/' + $name)
    if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination $PackageDirectory }
}
Copy-Item -LiteralPath (Join-Path $RepoRoot 'LICENSE') -Destination (Join-Path $PackageDirectory 'LICENSE-codex.txt')
Copy-Item -LiteralPath (Join-Path $RepoRoot 'NOTICE') -Destination (Join-Path $PackageDirectory 'NOTICE-codex.txt')
Copy-Item -Path (Join-Path $PSScriptRoot '*.ps1'),(Join-Path $PSScriptRoot '*.py'),(Join-Path $PSScriptRoot '*.cs') -Destination (Join-Path $PackageDirectory 'windows')
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
& $compiler /nologo /target:exe /platform:x64 "/out:$(Join-Path $PackageDirectory 'codex-custom.exe')" (Join-Path $PSScriptRoot 'Launcher.cs')
if ($LASTEXITCODE) { throw 'C# launcher compilation failed.' }
& $compiler /nologo /target:winexe /platform:x64 /reference:System.Windows.Forms.dll "/out:$(Join-Path $PackageDirectory 'Codex-Custom-Desktop.exe')" (Join-Path $PSScriptRoot 'DesktopLauncher.cs')
if ($LASTEXITCODE) { throw 'C# desktop launcher compilation failed.' }
$files = @{}
Get-ChildItem -LiteralPath $PackageDirectory -File -Recurse | Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' } | ForEach-Object {
    $relative = $_.FullName.Substring($PackageDirectory.Length + 1).Replace('\', '/')
    $files[$relative] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
$manifest = [ordered]@{
    schema = 'codex-custom-package/v1'
    upstreamTag = 'rust-v0.157.1'
    sourceCommit = $nativeBuild.sourceCommit
    companionSourceCommit = (& git.exe -C $RepoRoot rev-parse HEAD).Trim()
    companionWorkingTreeDirty = @(& git.exe -C $RepoRoot status --porcelain -- tools/custom-models).Count -gt 0
    target = $Target
    rustToolchain = '1.95.0-x86_64-pc-windows-msvc'
    buildLockSha256 = $nativeBuild.buildLockSha256
    v8 = @{ version = $nativeBuild.v8Version; manifestSha256 = $nativeBuild.v8ManifestSha256 }
    pythonVersion = '3.13.7'
    pythonArchiveSha256 = 'f6cca216a359be84797cabb54149ce5e062afb16cc7567eb7fc51cacb2d86b65'
    ripgrep = @{ version = $rgVersion; licenseArchiveUrl = $rgUrl; licenseArchiveSha256 = $rgSpec.digest; sourceDesktopVersion = $desktop.Version.ToString(); publisher = $rgSignature.SignerCertificate.Subject }
    files = $files
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $PackageDirectory 'manifest.json') -Encoding utf8
& (Join-Path $PackageDirectory 'windows/revalidate.ps1') -PackageDirectory $PackageDirectory
if (!$?) { throw 'Package validation failed.' }
$archive = $PackageDirectory + '.zip'
Compress-Archive -Path (Join-Path $PackageDirectory '*') -DestinationPath $archive
Write-Host "Portable package: $archive"
Get-FileHash -LiteralPath $archive -Algorithm SHA256
