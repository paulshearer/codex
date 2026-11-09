. "$PSScriptRoot/validation-common.ps1"

function Assert-Rejected([scriptblock]$Action) {
    $rejected = $false
    try { & $Action | Out-Null } catch { $rejected = $true }
    if (!$rejected) { throw 'An invalid desktop manifest or fingerprint was accepted.' }
}

$fixtureRoot = [IO.Path]::GetFullPath((Join-Path ([IO.Path]::GetTempPath()) ('codex desktop fixture ' + [Guid]::NewGuid().ToString('N'))))
New-Item -ItemType Directory -Path (Join-Path $fixtureRoot 'app') | Out-Null
try {
    [IO.File]::WriteAllText((Join-Path $fixtureRoot 'app/ChatGPT.exe'), 'main executable fixture')
    [IO.File]::WriteAllText((Join-Path $fixtureRoot 'app/Codex.exe'), 'command stub fixture')
    $package = [pscustomobject]@{ InstallLocation = $fixtureRoot; PackageFullName = 'OpenAI.Codex_fixture'; PackageFamilyName = 'OpenAI.Codex_fixture_family' }
    $manifest = [xml]'<Package><Applications><Application Id="App" Executable="app/ChatGPT.exe" EntryPoint="Windows.FullTrustApplication" /><Application Id="CodexCoreCommandRunner" Executable="app/resources/codex-command-runner.exe" /></Applications></Package>'
    $resolved = Resolve-DesktopApplication $package $manifest
    if ($resolved.executable -ne (Join-Path $fixtureRoot 'app/ChatGPT.exe') -or $resolved.applicationId -ne 'App') { throw 'Manifest discovery selected the command stub instead of the main application.' }
    foreach ($invalid in @('../outside.exe', (Join-Path $fixtureRoot 'app/ChatGPT.exe'), 'app/missing.exe', 'app/not-executable.txt')) {
        $manifest.Package.Applications.Application[0].SetAttribute('Executable', $invalid)
        Assert-Rejected { Resolve-DesktopApplication $package $manifest }
    }
    Assert-Rejected { Resolve-DesktopApplication $package ([xml]'<Package><Applications><Application Id="Other" Executable="app/ChatGPT.exe" /></Applications></Package>') }
    Assert-Rejected { Resolve-DesktopApplication $package ([xml]'<Package><Applications><Application Id="App" Executable="app/ChatGPT.exe" /><Application Id="App" Executable="app/Codex.exe" /></Applications></Package>') }
} finally {
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (!$fixtureRoot.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Desktop fixture cleanup path escaped the temporary directory.' }
    Remove-Item -LiteralPath $fixtureRoot -Recurse -Force
}

$fingerprint = Get-DesktopFingerprint | ConvertTo-Json -Depth 6 | ConvertFrom-Json
if ((Assert-DesktopFingerprint $fingerprint) -ne $fingerprint.executable) { throw 'Desktop fingerprint returned the wrong launch executable.' }
$fingerprint.packageFullName += '_changed'
Assert-Rejected { Assert-DesktopFingerprint $fingerprint }
$fingerprint = Get-DesktopFingerprint | ConvertTo-Json -Depth 6 | ConvertFrom-Json
$mainName = [IO.Path]::GetFileName($fingerprint.executable)
$fingerprint.files.PSObject.Properties[$mainName].Value = ('0' * 64)
Assert-Rejected { Assert-DesktopFingerprint $fingerprint }
$fingerprint.files.PSObject.Properties.Remove($mainName)
Assert-Rejected { Assert-DesktopFingerprint $fingerprint }
Write-Host 'Desktop manifest main executable, containment, identity and executable hash verification passed.'
