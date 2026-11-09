[CmdletBinding()]
param(
    [string]$KeystorePath = (Join-Path $env:USERPROFILE '.permissive-codex-android-secrets\permissive-codex-release.p12'),
    [string]$Alias = 'permissive-codex',
    [string]$PasswordFile = (Join-Path $env:USERPROFILE '.permissive-codex-android-secrets\permissive-codex-release-password.dpapi'),
    [switch]$WithUiTests
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\android-env.ps1"
$resolvedKeystore = (Resolve-Path -LiteralPath $KeystorePath).Path
$resolvedPasswordFile = (Resolve-Path -LiteralPath $PasswordFile).Path
$securePassword = Get-Content -LiteralPath $resolvedPasswordFile -Raw | ConvertTo-SecureString
$passwordBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
$signingFile = Join-Path ([IO.Path]::GetTempPath()) ("permissive-codex-signing-{0}.properties" -f [Guid]::NewGuid())
try {
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordBstr)
    $signingLines = @(
        "storeFile=$($resolvedKeystore.Replace('\', '\\'))"
        "storePassword=$plainPassword"
        "keyAlias=$Alias"
        "keyPassword=$plainPassword"
    )
    [IO.File]::WriteAllLines($signingFile, $signingLines, [Text.UTF8Encoding]::new($false))
    $env:PERMISSIVE_CODEX_SIGNING_PROPERTIES = $signingFile
    $gradleTasks = @('testDebugUnitTest', 'lintDebug', 'lintRelease')
    if ($WithUiTests) { $gradleTasks += 'connectedDebugAndroidTest' }
    $gradleTasks += 'assembleRelease'
    & (Join-Path $PSScriptRoot '..\gradlew.bat') -p (Join-Path $PSScriptRoot '..') @gradleTasks `
        '-Pkotlin.incremental=false' `
        '-Pksp.incremental=false' `
        '-Pkotlin.compiler.execution.strategy=in-process' `
        --no-daemon --max-workers=1 --console=plain
    if ($LASTEXITCODE -ne 0) { throw 'Release validation or build failed.' }
} finally {
    Remove-Item Env:PERMISSIVE_CODEX_SIGNING_PROPERTIES -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $signingFile) {
        $length = (Get-Item -LiteralPath $signingFile).Length
        if ($length -gt 0) { [IO.File]::WriteAllBytes($signingFile, (New-Object byte[] $length)) }
        Remove-Item -LiteralPath $signingFile -Force
    }
    $plainPassword = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordBstr)
}

$apkPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\app\build\outputs\apk\release\app-release.apk')).Path
& $script:PermissiveAndroidTools.ApkSigner verify --verbose --print-certs $apkPath
if ($LASTEXITCODE -ne 0) { throw 'Release APK signature verification failed.' }
$outputDirectory = Join-Path $PSScriptRoot '..\out'
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$deliveryPath = Join-Path $outputDirectory 'PermissiveCodex-v0.1.12-release.apk'
Copy-Item -LiteralPath $apkPath -Destination $deliveryPath -Force
[pscustomobject]@{
    apkPath = (Resolve-Path -LiteralPath $deliveryPath).Path
    sha256 = (Get-FileHash -LiteralPath $deliveryPath -Algorithm SHA256).Hash
} | ConvertTo-Json
