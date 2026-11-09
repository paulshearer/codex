param(
    [ValidateSet('Status', 'Launch', 'CleanupSession', 'StoreMasterPassword', 'UnlockFromCredentialManager', 'DeleteMasterPassword')]
    [string]$Action = 'Status',

    [string]$Url = 'about:blank',

    [int]$Port = 9224,

    [string]$ProfileDirectory = 'Default',

    [string]$UserDataDirectory = '',

    [string]$CredentialTarget = 'Codex.EdgeBitwarden.MasterPassword',

    [string]$CredentialUserName = 'paul@informedcio.com'
)

$ErrorActionPreference = 'Stop'

$edgePath = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
if (-not (Test-Path -LiteralPath $edgePath)) {
    $edgePath = 'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
}

$bwPath = 'C:\Users\paul\AppData\Local\Microsoft\WinGet\Packages\Bitwarden.CLI_Microsoft.Winget.Source_8wekyb3d8bbwe\bw.exe'
$sessionPath = Join-Path $env:TEMP 'bw-session.txt'

$credentialManagerLoaded = $false

function Ensure-CredentialManagerType {
    if ($script:credentialManagerLoaded) {
        return
    }
    if ('CodexCredentialManager' -as [type]) {
        $script:credentialManagerLoaded = $true
        return
    }

    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class CodexCredentialManager {
    public const int CRED_TYPE_GENERIC = 1;
    public const int CRED_PERSIST_LOCAL_MACHINE = 2;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct CREDENTIAL {
        public int Flags;
        public int Type;
        public string TargetName;
        public string Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public int CredentialBlobSize;
        public IntPtr CredentialBlob;
        public int Persist;
        public int AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }

    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredWrite(ref CREDENTIAL userCredential, int flags);

    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredRead(string target, int type, int reservedFlag, out IntPtr credentialPtr);

    [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern bool CredDelete(string target, int type, int flags);

    [DllImport("advapi32.dll", EntryPoint = "CredFree", SetLastError = true)]
    public static extern void CredFree(IntPtr buffer);
}
'@

    $script:credentialManagerLoaded = $true
}

function ConvertFrom-SecureStringToPlainText {
    param([securestring]$SecureString)

    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureString)
    try {
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
}

function Test-StoredCredential {
    param([string]$TargetName)

    Ensure-CredentialManagerType
    $credentialPtr = [IntPtr]::Zero
    $found = [CodexCredentialManager]::CredRead($TargetName, [CodexCredentialManager]::CRED_TYPE_GENERIC, 0, [ref]$credentialPtr)
    if ($credentialPtr -ne [IntPtr]::Zero) {
        [CodexCredentialManager]::CredFree($credentialPtr)
    }

    return $found
}

function Set-StoredSecret {
    param(
        [string]$TargetName,
        [string]$UserName,
        [string]$Secret
    )

    Ensure-CredentialManagerType
    $secretBytes = [Text.Encoding]::Unicode.GetBytes($Secret)
    $secretPtr = [Runtime.InteropServices.Marshal]::AllocCoTaskMem($secretBytes.Length)

    try {
        [Runtime.InteropServices.Marshal]::Copy($secretBytes, 0, $secretPtr, $secretBytes.Length)

        $credential = New-Object CodexCredentialManager+CREDENTIAL
        $credential.Type = [CodexCredentialManager]::CRED_TYPE_GENERIC
        $credential.TargetName = $TargetName
        $credential.UserName = $UserName
        $credential.CredentialBlobSize = $secretBytes.Length
        $credential.CredentialBlob = $secretPtr
        $credential.Persist = [CodexCredentialManager]::CRED_PERSIST_LOCAL_MACHINE

        if (-not [CodexCredentialManager]::CredWrite([ref]$credential, 0)) {
            $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
            throw "CredWrite failed with Win32 error $errorCode."
        }
    }
    finally {
        if ($secretPtr -ne [IntPtr]::Zero) {
            for ($i = 0; $i -lt $secretBytes.Length; $i++) {
                [Runtime.InteropServices.Marshal]::WriteByte($secretPtr, $i, 0)
            }
            [Runtime.InteropServices.Marshal]::FreeCoTaskMem($secretPtr)
        }
        if ($secretBytes) {
            [Array]::Clear($secretBytes, 0, $secretBytes.Length)
        }
    }
}

function Get-StoredSecret {
    param([string]$TargetName)

    Ensure-CredentialManagerType
    $credentialPtr = [IntPtr]::Zero
    $found = [CodexCredentialManager]::CredRead($TargetName, [CodexCredentialManager]::CRED_TYPE_GENERIC, 0, [ref]$credentialPtr)
    if (-not $found) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        throw "Credential '$TargetName' was not found in Windows Credential Manager. Win32 error $errorCode."
    }

    try {
        $credential = [Runtime.InteropServices.Marshal]::PtrToStructure($credentialPtr, [type][CodexCredentialManager+CREDENTIAL])
        $secretBytes = New-Object byte[] $credential.CredentialBlobSize
        [Runtime.InteropServices.Marshal]::Copy($credential.CredentialBlob, $secretBytes, 0, $credential.CredentialBlobSize)
        [Text.Encoding]::Unicode.GetString($secretBytes)
    }
    finally {
        if ($secretBytes) {
            [Array]::Clear($secretBytes, 0, $secretBytes.Length)
        }
        if ($credentialPtr -ne [IntPtr]::Zero) {
            [CodexCredentialManager]::CredFree($credentialPtr)
        }
    }
}

function Remove-StoredSecret {
    param([string]$TargetName)

    Ensure-CredentialManagerType
    $deleted = [CodexCredentialManager]::CredDelete($TargetName, [CodexCredentialManager]::CRED_TYPE_GENERIC, 0)
    if (-not $deleted) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        if ($errorCode -ne 1168) {
            throw "CredDelete failed with Win32 error $errorCode."
        }
    }

    return $deleted
}

function Get-BitwardenStatusSummary {
    param([string]$Session)

    if (-not (Test-Path -LiteralPath $bwPath)) {
        return 'Bitwarden CLI not found'
    }

    $statusRaw = if ($Session) {
        & $bwPath status --session $Session
    }
    else {
        & $bwPath status
    }

    try {
        $parsed = $statusRaw | ConvertFrom-Json
        [pscustomobject]@{
            status = $parsed.status
            userEmail = $parsed.userEmail
            lastSync = $parsed.lastSync
            serverUrl = $parsed.serverUrl
        }
    }
    catch {
        $statusRaw
    }
}

function Get-CdpPages {
    param([int]$CdpPort)

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$CdpPort/json/list" -TimeoutSec 2
        return ($response.Content | ConvertFrom-Json)
    }
    catch {
        return @()
    }
}

function ConvertTo-CdpPageSummary {
    param($Page)

    $safeUrl = ''
    if ($Page.url) {
        try {
            $uri = [uri]$Page.url
            if ($uri.Scheme -in @('http', 'https')) {
                $path = $uri.AbsolutePath
                if ($path -match ';') {
                    $path = ($path -split ';')[0]
                }
                if ($path.Length -gt 80) {
                    $path = $path.Substring(0, 80) + '...'
                }
                $safeUrl = "$($uri.Scheme)://$($uri.Host)$path"
            }
            elseif ($Page.url.Length -gt 120) {
                $safeUrl = $Page.url.Substring(0, 120) + '...'
            }
            else {
                $safeUrl = $Page.url
            }
        }
        catch {
            $safeUrl = if ($Page.url.Length -gt 120) { $Page.url.Substring(0, 120) + '...' } else { $Page.url }
        }
    }

    $safeTitle = [string]$Page.title
    if ($safeTitle -match '^https?://') {
        try {
            $titleUri = [uri]$safeTitle
            $titlePath = $titleUri.AbsolutePath
            if ($titlePath -match ';') {
                $titlePath = ($titlePath -split ';')[0]
            }
            if ($titlePath.Length -gt 80) {
                $titlePath = $titlePath.Substring(0, 80) + '...'
            }
            $safeTitle = "$($titleUri.Scheme)://$($titleUri.Host)$titlePath"
        }
        catch {
            $safeTitle = 'URL page'
        }
    }
    if ($safeTitle.Length -gt 120) {
        $safeTitle = $safeTitle.Substring(0, 120) + '...'
    }

    [pscustomobject]@{
        title = $safeTitle
        url = $safeUrl
    }
}

function Get-EdgeProfileSummary {
    $statePaths = @(
        (Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\User Data\Local State'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\User\Local State')
    ) | Where-Object { Test-Path -LiteralPath $_ }

    $rows = @()
    foreach ($statePath in $statePaths) {
        try {
            $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
            foreach ($profile in $state.profile.info_cache.PSObject.Properties) {
                $rows += [pscustomobject]@{
                    LocalState = $statePath
                    ProfileDir = $profile.Name
                    Name = $profile.Value.name
                    UserName = $profile.Value.user_name
                    GaiaName = $profile.Value.gaia_name
                }
            }
        }
        catch {
            $rows += [pscustomobject]@{
                LocalState = $statePath
                ProfileDir = ''
                Name = ''
                UserName = ''
                GaiaName = "error: $($_.Exception.Message)"
            }
        }
    }

    return $rows
}

switch ($Action) {
    'Status' {
        $session = $null
        if (Test-Path -LiteralPath $sessionPath) {
            $session = (Get-Content -Raw -LiteralPath $sessionPath).Trim()
        }

        $bwStatus = Get-BitwardenStatusSummary -Session $session

        $windows = Get-Process msedge -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowTitle } |
            Select-Object Id, MainWindowTitle

        $summary = [pscustomobject]@{
            EdgePath = $edgePath
            BitwardenCliPath = $bwPath
            SessionFilePresent = (Test-Path -LiteralPath $sessionPath)
            BitwardenStatus = $bwStatus
            CredentialManager = [pscustomobject]@{
                Target = $CredentialTarget
                UserName = $CredentialUserName
                MasterPasswordStored = (Test-StoredCredential -TargetName $CredentialTarget)
            }
            EdgeProfiles = @(Get-EdgeProfileSummary)
            EdgeWindows = @($windows)
            CdpPages9223 = @(Get-CdpPages -CdpPort 9223 | ForEach-Object { ConvertTo-CdpPageSummary $_ })
            CdpPages9224 = @(Get-CdpPages -CdpPort 9224 | ForEach-Object { ConvertTo-CdpPageSummary $_ })
        }

        $summary | ConvertTo-Json -Depth 8
    }

    'Launch' {
        if (-not (Test-Path -LiteralPath $edgePath)) {
            throw "Microsoft Edge executable not found."
        }

        $userDataArg = if ([string]::IsNullOrWhiteSpace($UserDataDirectory)) {
            ''
        }
        else {
            " --user-data-dir=`"$UserDataDirectory`""
        }
        $args = "--remote-debugging-port=$Port$userDataArg --profile-directory=`"$ProfileDirectory`" --new-window `"$Url`""
        Start-Process -FilePath $edgePath -WorkingDirectory (Split-Path $edgePath) -ArgumentList $args
        Start-Sleep -Seconds 3

        [pscustomobject]@{
            Launched = $true
            Port = $Port
            ProfileDirectory = $ProfileDirectory
            UserDataDirectory = $UserDataDirectory
            Url = $Url
            CdpPages = @(Get-CdpPages -CdpPort $Port | ForEach-Object { ConvertTo-CdpPageSummary $_ })
        } | ConvertTo-Json -Depth 5
    }

    'StoreMasterPassword' {
        $securePassword = Read-Host -Prompt "Bitwarden master password for $CredentialUserName" -AsSecureString
        $plainPassword = ConvertFrom-SecureStringToPlainText -SecureString $securePassword
        try {
            Set-StoredSecret -TargetName $CredentialTarget -UserName $CredentialUserName -Secret $plainPassword
            [pscustomobject]@{
                Stored = $true
                Target = $CredentialTarget
                UserName = $CredentialUserName
            } | ConvertTo-Json
        }
        finally {
            $plainPassword = $null
            $securePassword.Dispose()
        }
    }

    'UnlockFromCredentialManager' {
        if (-not (Test-Path -LiteralPath $bwPath)) {
            throw "Bitwarden CLI not found."
        }

        $existingSession = $null
        if (Test-Path -LiteralPath $sessionPath) {
            $existingSession = (Get-Content -Raw -LiteralPath $sessionPath).Trim()
            $existingStatus = Get-BitwardenStatusSummary -Session $existingSession
            if ($existingStatus.status -eq 'unlocked') {
                [pscustomobject]@{
                    Unlocked = $true
                    AlreadyUnlocked = $true
                    SessionFile = $sessionPath
                    BitwardenStatus = $existingStatus
                } | ConvertTo-Json -Depth 5
                break
            }
        }

        $masterPassword = Get-StoredSecret -TargetName $CredentialTarget
        try {
            $session = ($masterPassword + [Environment]::NewLine) | & $bwPath unlock --raw
            $session = ($session | Out-String).Trim()

            if (-not $session) {
                throw "Bitwarden unlock did not return a session token."
            }

            Set-Content -LiteralPath $sessionPath -Value $session -NoNewline
            $env:BW_SESSION = $session
            $status = Get-BitwardenStatusSummary -Session $session

            [pscustomobject]@{
                Unlocked = ($status.status -eq 'unlocked')
                AlreadyUnlocked = $false
                SessionFile = $sessionPath
                BitwardenStatus = $status
            } | ConvertTo-Json -Depth 5
        }
        finally {
            $masterPassword = $null
            $session = $null
        }
    }

    'DeleteMasterPassword' {
        $deleted = Remove-StoredSecret -TargetName $CredentialTarget
        [pscustomobject]@{
            Deleted = $deleted
            Target = $CredentialTarget
        } | ConvertTo-Json
    }

    'CleanupSession' {
        if (Test-Path -LiteralPath $sessionPath) {
            Remove-Item -LiteralPath $sessionPath -Force
            [pscustomobject]@{ Deleted = $true; Path = $sessionPath } | ConvertTo-Json
        }
        else {
            [pscustomobject]@{ Deleted = $false; Path = $sessionPath } | ConvertTo-Json
        }
    }
}
