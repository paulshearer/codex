#requires -Version 5.1
[CmdletBinding(DefaultParameterSetName='File')]
param([Parameter(Mandatory)][string]$BindingPath,
      [Parameter(Mandatory,ParameterSetName='File')][string]$RequestPath,
      [Parameter(Mandatory,ParameterSetName='Base64')][string]$RequestBase64)
$ErrorActionPreference = 'Stop'
try {
    . (Join-Path $PSScriptRoot 'common.ps1')
    Assert-SandboxAdministrator
    # Validate the envelope without logging its content. It remains a data argument.
    $request = if ($PSCmdlet.ParameterSetName -eq 'File') {Get-Content -LiteralPath $RequestPath -Raw} else {[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($RequestBase64))}
    $null = $request | ConvertFrom-Json
    $binding = Read-SandboxBinding $BindingPath
    Test-SandboxPerimeter $binding | Out-Null
    $credential = Get-SandboxCredential $binding
    $receivedBytes = 0L
    Invoke-Command -VMId ([Guid]$binding.vmId) -Credential $credential -ScriptBlock {
        param([string]$Envelope)
        $ErrorActionPreference = 'Stop'
        $env:PERMISSIVE_PACKAGE = 'C:\CodexPermissive'
        $env:PERMISSIVE_HOME = 'C:\ProgramData\CodexPermissiveGuest'
        $env:PERMISSIVE_DEPLOYMENT = 'C:\CodexPermissive\deployment.json'
        $env:PERMISSIVE_ROLE = 'guest'
        $start = New-Object Diagnostics.ProcessStartInfo
        $start.FileName = 'C:\CodexPermissive\python\python.exe'
        $start.Arguments = '-m permissive.guest_bridge'
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardInput = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $start.StandardOutputEncoding = [Text.Encoding]::UTF8
        $start.EnvironmentVariables['PYTHONUTF8'] = '1'
        $process = New-Object Diagnostics.Process
        $process.StartInfo = $start
        try {
            if (!$process.Start()) { throw 'Guest bridge could not start.' }
            $errors = $process.StandardError.ReadToEndAsync()
            $inputBytes = [Text.Encoding]::UTF8.GetBytes($Envelope)
            try { $process.StandardInput.BaseStream.Write($inputBytes,0,$inputBytes.Length); $process.StandardInput.Close() } finally { [Array]::Clear($inputBytes,0,$inputBytes.Length) }
            $buffer = New-Object char[] 32768
            $sentBytes = 0L
            while (($count = $process.StandardOutput.Read($buffer,0,$buffer.Length)) -gt 0) {
                $chunk = New-Object string($buffer,0,$count)
                $sentBytes += [Text.Encoding]::UTF8.GetByteCount($chunk)
                if ($sentBytes -gt 64MB) { $process.Kill(); throw 'Guest bridge exceeded the transport budget.' }
                Write-Output -NoEnumerate $chunk
            }
            $process.WaitForExit()
            $null = $errors.GetAwaiter().GetResult()
            if ($process.ExitCode) { throw 'Guest bridge execution failed.' }
        } finally { $process.Dispose() }
    } -ArgumentList $request | ForEach-Object {
        # Stream remoting output; never collect an unbounded guest array in a
        # host PowerShell variable before Python can enforce its own budget.
        if ($_ -isnot [string]) { throw 'Guest bridge returned an invalid output type.' }
        $receivedBytes += [Text.Encoding]::UTF8.GetByteCount($_)
        if ($receivedBytes -gt 64MB) { throw 'Guest response exceeded the host transport budget.' }
        [Console]::Out.Write($_)
    }
} catch {
    # Hypervisor errors can contain parameters; return a fixed message instead.
    [Console]::Error.WriteLine('Sandbox command failed. Verify its VM, perimeter and host-owned credential binding.')
    exit 1
}
