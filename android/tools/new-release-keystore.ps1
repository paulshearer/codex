[CmdletBinding()]
param(
    [string]$KeystorePath = (Join-Path $env:USERPROFILE '.permissive-codex-android-secrets\permissive-codex-release.p12'),
    [string]$Alias = 'permissive-codex',
    [string]$PasswordFile = (Join-Path $env:USERPROFILE '.permissive-codex-android-secrets\permissive-codex-release-password.dpapi')
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\android-env.ps1"
$resolvedPath = [IO.Path]::GetFullPath($KeystorePath)
$resolvedPasswordFile = [IO.Path]::GetFullPath($PasswordFile)
if (Test-Path -LiteralPath $resolvedPath) { throw "Release keystore already exists: $resolvedPath" }
if (Test-Path -LiteralPath $resolvedPasswordFile) { throw "Release password file already exists: $resolvedPasswordFile" }
$directory = Split-Path -Parent $resolvedPath
if ((Split-Path -Parent $resolvedPasswordFile) -ne $directory) {
    throw 'The keystore and DPAPI password file must be in the same protected directory.'
}
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$inheritance = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
$allow = [Security.AccessControl.AccessControlType]::Allow
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', $inheritance, 'None', $allow)))
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule('NT AUTHORITY\SYSTEM', 'FullControl', $inheritance, 'None', $allow)))
Set-Acl -LiteralPath $directory -AclObject $acl

$randomBytes = New-Object byte[] 32
$generator = [Security.Cryptography.RandomNumberGenerator]::Create()
$generator.GetBytes($randomBytes)
$generator.Dispose()
$generatedPassword = [Convert]::ToBase64String($randomBytes)
$securePassword = ConvertTo-SecureString $generatedPassword -AsPlainText -Force
$passwordBstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
try {
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordBstr)
    $securePassword | ConvertFrom-SecureString | Set-Content -LiteralPath $resolvedPasswordFile -Encoding ASCII -NoNewline
    $env:PERMISSIVE_CODEX_NEW_KEYSTORE_PASSWORD = $plainPassword
    $keytool = Join-Path $script:PermissiveAndroidTools.JavaHome 'bin\keytool.exe'
    & $keytool -genkeypair -keystore $resolvedPath -storetype PKCS12 `
        -storepass:env PERMISSIVE_CODEX_NEW_KEYSTORE_PASSWORD `
        -keypass:env PERMISSIVE_CODEX_NEW_KEYSTORE_PASSWORD `
        -alias $Alias -keyalg RSA -keysize 4096 -sigalg SHA256withRSA -validity 9125 `
        -dname 'CN=Permissive Codex, O=Private Android Deployment, C=US'
    if ($LASTEXITCODE -ne 0) { throw 'Release-keystore creation failed.' }
} catch {
    Remove-Item -LiteralPath $resolvedPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $resolvedPasswordFile -Force -ErrorAction SilentlyContinue
    throw
} finally {
    Remove-Item Env:PERMISSIVE_CODEX_NEW_KEYSTORE_PASSWORD -ErrorAction SilentlyContinue
    $plainPassword = $null
    $generatedPassword = $null
    [Array]::Clear($randomBytes, 0, $randomBytes.Length)
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordBstr)
}

[pscustomobject]@{
    keystorePath = $resolvedPath
    alias = $Alias
    passwordProtection = 'Windows DPAPI, current user'
    offlineBackupRequired = $true
} | ConvertTo-Json
