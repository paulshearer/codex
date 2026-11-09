[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^emulator-[0-9]+$')][string]$Serial)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\android-env.ps1"
$state = & $script:PermissiveAndroidTools.Adb -s $Serial get-state
if ($LASTEXITCODE -ne 0 -or $state -ne 'device') { throw 'The selected local emulator is not ready.' }
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
& (Join-Path $projectRoot 'gradlew.bat') -p $projectRoot assembleDebug assembleDebugAndroidTest --no-daemon --max-workers=2 --console=plain
if ($LASTEXITCODE -ne 0) { throw 'Android test APK build failed.' }
& $script:PermissiveAndroidTools.Adb -s $Serial install -r (Join-Path $projectRoot 'app\build\outputs\apk\debug\app-debug.apk')
if ($LASTEXITCODE -ne 0) { throw 'Emulator app installation failed.' }
& $script:PermissiveAndroidTools.Adb -s $Serial install -r (Join-Path $projectRoot 'app\build\outputs\apk\androidTest\debug\app-debug-androidTest.apk')
if ($LASTEXITCODE -ne 0) { throw 'Emulator test installation failed.' }
$testOutput = & $script:PermissiveAndroidTools.Adb -s $Serial shell am instrument -w -r com.pshea.permissivecodex.debug.test/androidx.test.runner.AndroidJUnitRunner
$testOutput | Write-Output
if ($LASTEXITCODE -ne 0 -or ($testOutput -join "`n") -notmatch 'OK \([0-9]+ tests?\)') { throw 'Android instrumentation failed.' }
