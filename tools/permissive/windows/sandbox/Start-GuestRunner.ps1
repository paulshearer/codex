#requires -Version 5.1
$ErrorActionPreference = 'Stop'
$package = 'C:\CodexPermissive'
$deployment = Get-Content -LiteralPath (Join-Path $package 'deployment.json') -Raw | ConvertFrom-Json
if ($deployment.role -ne 'guest') { throw 'Guest runner requires an explicit guest deployment.' }
$env:PERMISSIVE_PACKAGE = $package
$env:PERMISSIVE_HOME = 'C:\ProgramData\CodexPermissiveGuest'
$env:PERMISSIVE_DEPLOYMENT = Join-Path $package 'deployment.json'
$env:PERMISSIVE_ROLE = 'guest'
$env:SSL_CERT_FILE = 'C:\ProgramData\CodexPermissive\relay-ca.pem'
$env:REQUESTS_CA_BUNDLE = $env:SSL_CERT_FILE
$env:PATH = ((@('native','node','python','powershell') | ForEach-Object {Join-Path $package $_}) -join ';') + ';' + $env:PATH
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $package 'chromium'
Set-Location -LiteralPath $package
& (Join-Path $package 'python\python.exe') -B -X utf8 -m permissive.auth --home $env:PERMISSIVE_HOME init-local --ensure *> $null
if ($LASTEXITCODE) { throw 'Guest-local API identity initialization failed.' }
# No prompt-bearing process output is written to diagnostic files.
& (Join-Path $package 'python\python.exe') -m permissive serve --role guest --deployment $env:PERMISSIVE_DEPLOYMENT *> $null
exit $LASTEXITCODE
