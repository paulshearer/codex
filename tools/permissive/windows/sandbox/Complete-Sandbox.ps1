#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)][string]$BindingPath, [Parameter(Mandatory)][string]$RelayCertificatePath,
      [ValidateRange(30,1200)][int]$InitializationTimeoutSeconds = 1200)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SandboxAdministrator
$binding = Read-SandboxBinding $BindingPath
$vm = Get-VM -Id ([Guid]$binding.vmId)
$vm | Get-VMNetworkAdapter | Disconnect-VMNetworkAdapter
try {
    Update-SandboxNetworkBinding $binding
    Write-SandboxJson $BindingPath $binding
    Set-SandboxPerimeter $binding
    if ($vm.State -eq 'Off') { $vm | Start-VM }
    Initialize-SandboxGuest $binding (Resolve-Path -LiteralPath $RelayCertificatePath).Path $InitializationTimeoutSeconds
    Test-SandboxPerimeter $binding | Out-Null
    $vm | Get-VMNetworkAdapter | Connect-VMNetworkAdapter -SwitchName $binding.switchName
    Test-SandboxPerimeter $binding | Out-Null
    if ($binding.status -eq 'awaiting-relay' -and !@($vm | Get-VMSnapshot | Where-Object {$_.Name -eq 'permissive-baseline'}).Count) {
        # A cold baseline is portable across host CPU vendors and stores no RAM.
        $vm | Stop-VM
        if ((Get-VM -Id $vm.Id).State -ne 'Off') { throw 'Guest must shut down before its initial baseline.' }
        Set-VM -VM $vm -CheckpointType Standard
        $vm | Checkpoint-VM -SnapshotName 'permissive-baseline'
        Set-VM -VM $vm -CheckpointType ProductionOnly
        $vm | Start-VM
    }
    $binding.status = 'ready'
    Write-SandboxJson $BindingPath $binding
    Install-SandboxControllerBinding $BindingPath $binding.packagePath
} catch {
    $vm | Get-VMNetworkAdapter | Disconnect-VMNetworkAdapter
    throw
}
Write-Output $BindingPath
