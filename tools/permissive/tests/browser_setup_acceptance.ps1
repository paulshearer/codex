param([string]$Helper = 'C:/codex-build/browser-extension-setup.exe',
      [string]$Extension = 'C:/git/codex_permissive/tools/permissive/browser/extension')
$ErrorActionPreference = 'Stop'
$taskData = Join-Path 'C:/codex-build' ('permissive-browser-setup-acceptance-' + [Guid]::NewGuid().ToString('N'))
$taskProfile = Join-Path $taskData 'Default'
New-Item -ItemType Directory -Path $taskProfile | Out-Null
@{signin=@{allowed=$false};extensions=@{ui=@{developer_mode=$false}}} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $taskProfile 'Preferences') -Encoding utf8
$edge = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
try {
    Start-Process -FilePath $edge -ArgumentList ('--user-data-dir="' + $taskData + '" --no-first-run --disable-sync about:blank') -WindowStyle Normal
    Start-Sleep -Seconds 3
    $result = & $Helper $edge Default $Extension ('local-' + ('b' * 24)) 'Automatic setup acceptance' $taskData
    $result | Set-Content -LiteralPath (Join-Path $taskData 'setup-result.json')
    if ($LASTEXITCODE) { throw ($result -join "`n") }
    $report = $result | ConvertFrom-Json
    if (!$report.extensionLoaded -or !$report.paired -or !$report.enabledDeveloperMode) { throw 'Incomplete setup result.' }
    $profileArgument = '--user-data-dir="?' + [Regex]::Escape($taskData.Replace('/','\')) + '(?:"|\s|$)'
    $taskProcesses = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'msedge.exe' -and $_.CommandLine -and $_.CommandLine.Replace('/','\') -match $profileArgument -and $_.CommandLine -notlike '*--type=*' })
    foreach ($taskProcess in $taskProcesses) { $null = (Get-Process -Id $taskProcess.ProcessId).CloseMainWindow() }
    Start-Sleep -Seconds 3
    $preferences = Get-Content -Raw -LiteralPath (Join-Path $taskProfile 'Preferences') | ConvertFrom-Json
    $secure = Get-Content -Raw -LiteralPath (Join-Path $taskProfile 'Secure Preferences') | ConvertFrom-Json
    if (!$preferences.extensions.ui.developer_mode -and !$secure.extensions.ui.developer_mode) { throw 'Developer mode was not enabled.' }
    if (!$secure.extensions.settings.mnldpjghemdjnoefihnifnpfhlaihamk) { throw 'Extension was not persisted.' }
    @{result=$report;dataDirectory=$taskData;developerModeVerified=$true;extensionPersisted=$true} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath C:/codex-build/permissive-browser-setup-native-verification.json
    Write-Host 'Native Edge one-click extension setup passed from Developer mode off.'
} finally {
    # Only the isolated browser created by this acceptance case is terminated.
    $profileArgument = '--user-data-dir="?' + [Regex]::Escape($taskData.Replace('/','\')) + '(?:"|\s|$)'
    $taskProcesses = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'msedge.exe' -and $_.CommandLine -and $_.CommandLine.Replace('/','\') -match $profileArgument -and $_.CommandLine -notlike '*--type=*' })
    foreach ($taskProcess in $taskProcesses) { & taskkill.exe /PID $taskProcess.ProcessId /T /F | Out-Null }
}
