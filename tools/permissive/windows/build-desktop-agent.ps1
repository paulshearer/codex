param(
    [string]$OutputPath = (Join-Path $PSScriptRoot '../native/desktop-agent.exe'),
    [string]$SourcePath = (Join-Path $PSScriptRoot 'DesktopAgent.cs')
)
$ErrorActionPreference = 'Stop'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
if (!(Test-Path -LiteralPath $compiler -PathType Leaf)) { throw 'The Windows .NET Framework x64 C# compiler is unavailable.' }
$framework = Split-Path $compiler -Parent
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
New-Item -ItemType Directory -Path (Split-Path $OutputPath -Parent) -Force | Out-Null
& $compiler /nologo /target:exe /platform:x64 /optimize+ "/out:$OutputPath" "/reference:$framework/WPF/UIAutomationClient.dll" "/reference:$framework/WPF/UIAutomationTypes.dll" "/reference:$framework/WPF/WindowsBase.dll" /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Web.Extensions.dll $SourcePath
if ($LASTEXITCODE) { throw 'Desktop agent compilation failed.' }
Write-Output $OutputPath
