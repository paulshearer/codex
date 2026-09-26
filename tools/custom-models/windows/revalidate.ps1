param([string]$PackageDirectory = (Split-Path $PSScriptRoot))
. "$PSScriptRoot/validation-common.ps1"
$PackageDirectory = (Resolve-Path -LiteralPath $PackageDirectory).Path
$manifestHash = Assert-PackageHashes $PackageDirectory
& (Join-Path $PackageDirectory 'windows/verify-desktop.ps1')
if (!$?) { throw 'Desktop manifest discovery verification failed.' }
$python = Join-Path $PackageDirectory 'python/python.exe'
& $python -B (Join-Path $PackageDirectory 'windows/verify-launcher.py') --package $PackageDirectory
if ($LASTEXITCODE) { throw 'Windows launcher verification failed.' }
# Disable bytecode writes so validation leaves the pinned runtime tree intact.
& $python -B -m unittest discover -s (Join-Path $PackageDirectory 'tests') -v
if ($LASTEXITCODE) { throw 'Companion regression tests failed; this desktop version remains unvalidated.' }
& $python -B (Join-Path $PackageDirectory 'windows/smoke.py') --package $PackageDirectory --registry (Join-Path $PackageDirectory 'registry.example.json')
if ($LASTEXITCODE) { throw 'Native transport smoke failed; this desktop version remains unvalidated.' }
foreach ($name in @('native-acceptance.py', 'cli-acceptance.py', 'advanced-acceptance.py')) {
    $acceptance = Join-Path $PackageDirectory $name
    if (!(Test-Path -LiteralPath $acceptance)) { throw "The $name script is required before validating a package." }
    & $python -B $acceptance --native (Join-Path $PackageDirectory 'native/codex.exe') --launcher (Join-Path $PackageDirectory 'codex-custom.exe')
    if ($LASTEXITCODE) { throw "$name failed; this desktop version remains unvalidated." }
}
$fingerprint = Get-DesktopFingerprint
[ordered]@{ schema = 'codex-custom-desktop-validation/v1'; validatedAt = [DateTime]::UtcNow.ToString('o'); packageManifestSha256 = $manifestHash; desktop = $fingerprint } |
    ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $PackageDirectory 'desktop-validation.json') -Encoding utf8
Write-Host "Validated Codex desktop $($fingerprint.version) and the portable runtime."
