param([string]$BuildDirectory = 'C:\codex-build\standalone-0.162.1',
      [string]$DesktopBuildDirectory = 'C:\codex-build\permissive-desktop',
      [string]$OutputDirectory = 'C:\codex-build\CodexPermissive-Tauri-1.0.0',
      [string]$Python = 'C:\codex-build\permissive-service-venv\Scripts\python.exe',
      [string]$OcrDirectory = 'C:\Program Files\Tesseract-OCR')
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$repo = (Resolve-Path (Join-Path $root '../..')).Path
$nativeBuild = Get-Content -Raw -LiteralPath (Join-Path $BuildDirectory 'agent-build.json') | ConvertFrom-Json
if ($nativeBuild.upstreamTag -ne 'rust-v0.162.1') { throw 'Build the pinned 0.162.1 agent first.' }
if (!$nativeBuild.permissiveBuild) { throw 'The separately compiled Permissive runtime is required.' }
if (!$nativeBuild.PSObject.Properties['privacyPolicy'] -or $nativeBuild.privacyPolicy -ne 'no-content-diagnostics-v1') { throw 'Rebuild the native runtime with the current privacy policy before packaging.' }
$desktopBuild = Get-Content -Raw -LiteralPath (Join-Path $DesktopBuildDirectory 'desktop-build.json') | ConvertFrom-Json
if ($desktopBuild.framework -ne 'tauri' -or $desktopBuild.privacyPolicy -ne 'no-content-diagnostics-v1' -or $desktopBuild.webviewPrivacyPolicy -ne 'disable-microsoft-crash-upload-v1') { throw 'A production Rust desktop build is required.' }
$desktopBinary = Join-Path $DesktopBuildDirectory 'release/CodexPermissive.exe'
if ((Get-FileHash -LiteralPath $desktopBinary).Hash.ToLowerInvariant() -ne $desktopBuild.binarySha256) { throw 'Desktop binary hash changed.' }
foreach ($source in $desktopBuild.sourceInputs.PSObject.Properties) {
    if ((Get-FileHash -LiteralPath (Join-Path $root $source.Name)).Hash.ToLowerInvariant() -ne $source.Value) { throw "Desktop build source differs from checkout: $($source.Name)" }
}
if (Test-Path -LiteralPath (Join-Path $OutputDirectory 'electron')) { throw 'Choose a fresh package directory for the Rust desktop.' }
$sourceInputs = @{}
$changedSources = @(& git.exe -C $repo diff --name-only HEAD -- codex-rs) + @(& git.exe -C $repo ls-files --others --exclude-standard -- codex-rs)
foreach ($file in $changedSources | Sort-Object -Unique) {
    if ($file -eq 'codex-rs/Cargo.lock' -or !(Test-Path -LiteralPath (Join-Path $repo $file) -PathType Leaf)) { continue }
    $digest = (Get-FileHash -LiteralPath (Join-Path $repo $file) -Algorithm SHA256).Hash.ToLowerInvariant()
    if ((Get-FileHash -LiteralPath (Join-Path $BuildDirectory ('source/' + $file)) -Algorithm SHA256).Hash.ToLowerInvariant() -ne $digest) { throw "Native build source differs from checkout: $file" }
    $sourceInputs[$file] = $digest
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
foreach ($name in @('native', 'lib', 'browser', 'client', 'node', 'licenses', 'ocr', 'ocr/tessdata')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $OutputDirectory $name) | Out-Null
}
$ocrBinary = Join-Path $OcrDirectory 'tesseract.exe'
$ocrData = Join-Path $OcrDirectory 'tessdata/eng.traineddata'
if ((Get-FileHash -LiteralPath $ocrBinary -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'ccd044d6cf16eaaad151260e1fcc5e3e1504cd1b8644e940b4f7ae3e315dd0d3') { throw 'Use the verified Tesseract 5.5.0.20241111 Windows build.' }
if ((Get-FileHash -LiteralPath $ocrData -Algorithm SHA256).Hash.ToLowerInvariant() -ne '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2') { throw 'Tesseract English model hash changed.' }
Copy-Item -LiteralPath $ocrBinary -Destination (Join-Path $OutputDirectory 'ocr/tesseract.exe') -Force
Copy-Item -Path (Join-Path $OcrDirectory '*.dll') -Destination (Join-Path $OutputDirectory 'ocr') -Force
Copy-Item -LiteralPath $ocrData -Destination (Join-Path $OutputDirectory 'ocr/tessdata/eng.traineddata') -Force
Copy-Item -LiteralPath (Join-Path $OcrDirectory 'doc') -Destination (Join-Path $OutputDirectory 'licenses/Tesseract') -Recurse -Force
@{version='5.5.0.20241111';language='eng';source='https://github.com/tesseract-ocr/tesseract/releases/tag/5.5.0';binarySha256='ccd044d6cf16eaaad151260e1fcc5e3e1504cd1b8644e940b4f7ae3e315dd0d3';englishSha256='7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2'} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OutputDirectory 'licenses/Tesseract-build.json') -Encoding utf8
foreach ($binary in $nativeBuild.binaries.PSObject.Properties) {
    $source = Join-Path $BuildDirectory ('x86_64-pc-windows-msvc/release/' + $binary.Name)
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $binary.Value) { throw "Native binary hash changed: $($binary.Name)" }
    Copy-Item -LiteralPath $source -Destination (Join-Path $OutputDirectory 'native') -Force
}
. (Join-Path $repo 'tools/custom-models/windows/common.ps1')
$powershellArchive = Get-VerifiedDownload 'https://github.com/PowerShell/PowerShell/releases/download/v7.6.6/PowerShell-7.6.6-win-x64.zip' (Join-Path $BuildDirectory 'downloads/PowerShell-7.6.6-win-x64.zip') '02fe458be20493fbdf43f61ea20610b811ee6c738ab1676c61b9cfcd1a33c860'
Expand-Archive -LiteralPath $powershellArchive -DestinationPath (Join-Path $OutputDirectory 'powershell') -Force
$pythonArchive = Get-VerifiedDownload 'https://www.python.org/ftp/python/3.13.7/python-3.13.7-embed-amd64.zip' (Join-Path $BuildDirectory 'downloads/python-3.13.7-embed-amd64.zip') 'f6cca216a359be84797cabb54149ce5e062afb16cc7567eb7fc51cacb2d86b65'
Expand-Archive -LiteralPath $pythonArchive -DestinationPath (Join-Path $OutputDirectory 'python') -Force
[IO.File]::WriteAllText((Join-Path $OutputDirectory 'python/python313._pth'), "python313.zip`n.`n../lib`nimport site`n", [Text.UTF8Encoding]::new($false))
& $Python -m pip install --target (Join-Path $OutputDirectory 'lib') --platform win_amd64 --python-version 3.13 --implementation cp --abi cp313 --only-binary=:all: -r (Join-Path $root 'requirements.txt') --upgrade
if ($LASTEXITCODE) { throw 'Embedded Python dependency installation failed.' }
foreach ($module in @(@((Join-Path $root 'permissive'), 'permissive'), @((Join-Path $repo 'tools/custom-models/custom_models'), 'custom_models'))) {
    & robocopy.exe $module[0] (Join-Path $OutputDirectory ('lib/' + $module[1])) /E /NFL /NDL /NJH /NJS /NP /XF '*.pyc' /XD __pycache__
    if ($LASTEXITCODE -gt 7) { throw 'Python source staging failed.' }
}
Copy-Item -LiteralPath (Join-Path $repo 'tools/custom-models/assets') -Destination (Join-Path $OutputDirectory 'lib') -Recurse -Force
Copy-Item -LiteralPath (Join-Path $root 'openapi.json') -Destination (Join-Path $OutputDirectory 'lib/openapi.json') -Force
Copy-Item -LiteralPath (Join-Path $root 'permissive/public_suffix_list.dat') -Destination (Join-Path $OutputDirectory 'lib/permissive/public_suffix_list.dat') -Force
Copy-Item -LiteralPath $desktopBinary -Destination (Join-Path $OutputDirectory 'CodexPermissive.exe') -Force
Copy-Item -Path (Join-Path $DesktopBuildDirectory 'licenses/*') -Destination (Join-Path $OutputDirectory 'licenses') -Force
Copy-Item -Path (Join-Path $root 'browser/*') -Destination (Join-Path $OutputDirectory 'browser') -Recurse -Force
Copy-Item -Path (Join-Path $root 'client/*') -Destination (Join-Path $OutputDirectory 'client') -Recurse -Force
Copy-Item -LiteralPath (Get-Command node.exe).Source -Destination (Join-Path $OutputDirectory 'node/node.exe') -Force
foreach ($dependency in @('playwright', 'playwright-core')) {
    & robocopy.exe (Join-Path $root "node_modules/$dependency") (Join-Path $OutputDirectory "node_modules/$dependency") /E /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -gt 7) { throw 'Browser dependency staging failed.' }
}
$browsers = Join-Path $env:LOCALAPPDATA 'ms-playwright'
& robocopy.exe $browsers (Join-Path $OutputDirectory 'chromium') /E /NFL /NDL /NJH /NJS /NP /XD firefox-* webkit-*
if ($LASTEXITCODE -gt 7) { throw 'Chromium staging failed.' }
$rgSpec = ((Get-Content -LiteralPath (Join-Path $repo 'scripts/codex_package/rg') -Raw) -replace '^#![^\n]*\n', '' | ConvertFrom-Json).platforms.'windows-x86_64'
$rgArchive = Get-VerifiedDownload $rgSpec.providers[0].url (Join-Path $BuildDirectory 'downloads/ripgrep.zip') $rgSpec.digest
$rgTemporary = Join-Path $BuildDirectory 'downloads/ripgrep'
Expand-Archive -LiteralPath $rgArchive -DestinationPath $rgTemporary -Force
Copy-Item -LiteralPath (Join-Path $rgTemporary $rgSpec.path) -Destination (Join-Path $OutputDirectory 'native/rg.exe') -Force
foreach ($name in @('COPYING', 'LICENSE-MIT', 'UNLICENSE')) {
    Copy-Item -LiteralPath (Join-Path (Split-Path (Join-Path $rgTemporary $rgSpec.path)) $name) -Destination (Join-Path $OutputDirectory ('licenses/ripgrep-' + $name)) -Force
}
$nodeVersion = (& node.exe --version).Trim()
Invoke-WebRequest -Uri "https://raw.githubusercontent.com/nodejs/node/$nodeVersion/LICENSE" -OutFile (Join-Path $OutputDirectory 'licenses/Node-LICENSE')
Copy-Item -LiteralPath (Join-Path $repo 'LICENSE'),(Join-Path $repo 'NOTICE') -Destination (Join-Path $OutputDirectory 'licenses') -Force
Copy-Item -LiteralPath (Join-Path $root 'README.md') -Destination $OutputDirectory -Force
Copy-Item -LiteralPath (Join-Path $root 'API_AUTH.md') -Destination $OutputDirectory -Force
New-Item -ItemType Directory -Force -Path (Join-Path $OutputDirectory 'desktop-rs/vendor') | Out-Null
Copy-Item -LiteralPath (Join-Path $root 'desktop-rs/vendor/PRIVACY_PATCH.md') -Destination (Join-Path $OutputDirectory 'desktop-rs/vendor') -Force
Copy-Item -LiteralPath $PSScriptRoot -Destination $OutputDirectory -Recurse -Force
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
& $compiler /nologo /target:exe /platform:x64 "/out:$(Join-Path $OutputDirectory 'codex-permissive.exe')" (Join-Path $PSScriptRoot 'Launcher.cs')
if ($LASTEXITCODE) { throw 'CLI launcher compilation failed.' }
& $compiler /nologo /target:exe /platform:x64 "/out:$(Join-Path $OutputDirectory 'native-host.exe')" (Join-Path $PSScriptRoot 'Launcher.cs')
if ($LASTEXITCODE) { throw 'Browser native-host compilation failed.' }
$framework = Split-Path $compiler -Parent
& $compiler /nologo /target:exe /platform:x64 "/out:$(Join-Path $OutputDirectory 'native/browser-extension-setup.exe')" "/reference:$framework/WPF/UIAutomationClient.dll" "/reference:$framework/WPF/UIAutomationTypes.dll" "/reference:$framework/WPF/WindowsBase.dll" /reference:System.Windows.Forms.dll (Join-Path $PSScriptRoot 'BrowserExtensionSetup.cs')
if ($LASTEXITCODE) { throw 'Browser extension setup helper compilation failed.' }
& (Join-Path $PSScriptRoot 'build-desktop-agent.ps1') -OutputPath (Join-Path $OutputDirectory 'native/desktop-agent.exe') | Out-Null
if (!(Test-Path -LiteralPath (Join-Path $OutputDirectory 'native/desktop-agent.exe') -PathType Leaf)) { throw 'Native desktop agent compilation did not produce its output.' }
$files = @{}
Get-ChildItem -LiteralPath $OutputDirectory -File -Recurse | Where-Object {$_.FullName -ne (Join-Path $OutputDirectory 'manifest.json')} | ForEach-Object {
    $files[$_.FullName.Substring($OutputDirectory.Length + 1).Replace('\','/')] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
@{schema='codex-permissive-package/v1';version='1.0.0';desktop=$desktopBuild;upstreamTag='rust-v0.162.1';upstreamCommit='092d3acd6bec3e3a14bdc7e7a2810ab628ab759d';permissiveBuild=$true;native=$nativeBuild;sourceInputs=$sourceInputs;files=$files} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'manifest.json') -Encoding utf8
Write-Host "Standalone package: $OutputDirectory"
