param([string]$PackageDirectory = (Split-Path $PSScriptRoot -Parent))
$ErrorActionPreference = 'Stop'
$manifest = Get-Content -Raw -LiteralPath (Join-Path $PackageDirectory 'manifest.json') | ConvertFrom-Json
$nativeInfo = $manifest.native
$desktopInfo = $manifest.desktop
if (!$desktopInfo -or $desktopInfo.framework -ne 'tauri' -or $desktopInfo.privacyPolicy -ne 'no-content-diagnostics-v1' -or $desktopInfo.webviewPrivacyPolicy -ne 'disable-microsoft-crash-upload-v1') { throw 'This package requires the production Rust desktop.' }
if (Test-Path -LiteralPath (Join-Path $PackageDirectory 'electron')) { throw 'The Rust desktop package must not contain Electron.' }
if (!$nativeInfo -or !$nativeInfo.PSObject.Properties['privacyPolicy'] -or $nativeInfo.privacyPolicy -ne 'no-content-diagnostics-v1') { throw 'This package requires a native runtime built with the current privacy policy.' }
$root = (Resolve-Path -LiteralPath $PackageDirectory).Path
foreach ($requiredHelper in @('lib/permissive/desktop_worker.py', 'native/desktop-agent.exe', 'lib/permissive/http_worker.py', 'lib/permissive/harness_policy.py')) {
    if (!(Test-Path -LiteralPath (Join-Path $root $requiredHelper) -PathType Leaf)) { throw "Required harness helper is missing: $requiredHelper" }
}
foreach ($entry in $manifest.files.PSObject.Properties) {
    $target = [IO.Path]::GetFullPath((Join-Path $root $entry.Name))
    if (!$target.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Manifest path escapes the package.' }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) { throw "Package hash mismatch: $($entry.Name)" }
}
& (Join-Path $root 'native/codex-app-server.exe') --help | Out-Null
if ($LASTEXITCODE) { throw 'Native startup failed.' }
& (Join-Path $root 'codex-permissive.exe') --help
if ($LASTEXITCODE) { throw 'Standalone CLI startup failed.' }
$version = & (Join-Path $root 'codex-permissive.exe') --version
if ($LASTEXITCODE -or $version -notlike 'Codex Permissive 1.0.0*') { throw 'Standalone CLI identity check failed.' }
Write-Host $version
& (Join-Path $root 'python/python.exe') -B -c "import pypdf, pypdfium2, PIL; from permissive.pdf_worker import TOOLS; assert len(TOOLS) == 2"
if ($LASTEXITCODE) { throw 'Bundled PDF text and rendering dependencies failed to load.' }
& (Join-Path $root 'python/python.exe') -B -c "from permissive.http_worker import TOOLS; from permissive.harness_policy import desktop_capability; assert TOOLS[0]['name'] == 'request'; assert desktop_capability()['status'] == 'available'"
if ($LASTEXITCODE) { throw 'Bundled HTTP or desktop harness helper failed to load.' }
$ocrVersion = @(& (Join-Path $root 'ocr/tesseract.exe') --version 2>&1)[0].ToString()
if ($LASTEXITCODE -or $ocrVersion -ne 'tesseract v5.5.0.20241111') { throw 'Bundled OCR engine failed to load.' }
$ocrLanguages = & (Join-Path $root 'ocr/tesseract.exe') --tessdata-dir (Join-Path $root 'ocr/tessdata') --list-langs
if ($LASTEXITCODE -or $ocrLanguages -notcontains 'eng') { throw 'Bundled OCR English data failed to load.' }
$desktopStart = [Diagnostics.ProcessStartInfo]::new()
$desktopStart.FileName = Join-Path $root 'CodexPermissive.exe'
$desktopStart.Arguments = '--version'
$desktopStart.UseShellExecute = $false
$desktopStart.CreateNoWindow = $true
$desktopStart.RedirectStandardOutput = $true
$desktopStart.RedirectStandardError = $true
$desktopProcess = [Diagnostics.Process]::new()
$desktopProcess.StartInfo = $desktopStart
if (!$desktopProcess.Start()) { throw 'Rust desktop identity process failed to start.' }
$desktopOutput = $desktopProcess.StandardOutput.ReadToEndAsync()
$desktopErrors = $desktopProcess.StandardError.ReadToEndAsync()
if (!$desktopProcess.WaitForExit(15000)) { $desktopProcess.Kill(); throw 'Rust desktop identity check timed out.' }
$desktopVersion = $desktopOutput.Result.Trim()
if ($desktopProcess.ExitCode -ne 0 -or $desktopErrors.Result.Length -ne 0 -or $desktopVersion -ne 'Codex Permissive 1.0.0 (Rust desktop; Tauri)') { throw 'Rust desktop identity check failed.' }
$desktopProcess.Dispose()
Write-Host $desktopVersion
$shellVersion = & (Join-Path $root 'powershell/pwsh.exe') --version
if ($LASTEXITCODE -or $shellVersion -ne 'PowerShell 7.6.6') { throw 'Bundled PowerShell identity check failed.' }
Write-Host 'Package integrity, Rust desktop, native startup, and standalone CLI passed.'
