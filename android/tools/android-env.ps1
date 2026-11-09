$ErrorActionPreference = 'Stop'

$toolsRoot = Join-Path $env:USERPROFILE '.androidexpenses-tools'
$sdkRoot = Join-Path $toolsRoot 'android-sdk'
$javaExe = Get-ChildItem -Path (Join-Path $toolsRoot 'jdk') -Recurse -Filter java.exe |
    Where-Object { $_.FullName -like '*\bin\java.exe' } |
    Select-Object -First 1

if (-not $javaExe) { throw "JDK was not found under $toolsRoot\jdk." }
if (-not (Test-Path -LiteralPath $sdkRoot)) { throw "Android SDK was not found at $sdkRoot." }

$javaHome = Split-Path -Parent (Split-Path -Parent $javaExe.FullName)
$env:JAVA_HOME = $javaHome
$env:ANDROID_SDK_ROOT = $sdkRoot
$env:ANDROID_HOME = $sdkRoot
$env:Path = "$javaHome\bin;$(Join-Path $sdkRoot 'cmdline-tools\latest\bin');$(Join-Path $sdkRoot 'platform-tools');$env:Path"

$script:PermissiveAndroidTools = [pscustomobject]@{
    JavaHome = $javaHome
    SdkRoot = $sdkRoot
    Adb = Join-Path $sdkRoot 'platform-tools\adb.exe'
    ApkSigner = Join-Path $sdkRoot 'build-tools\36.0.0\apksigner.bat'
}
