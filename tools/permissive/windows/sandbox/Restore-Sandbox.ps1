#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory)][string]$BindingPath)
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-SandboxAdministrator
$binding = Read-SandboxBinding $BindingPath
if (!$binding.PSObject.Properties['relayTls']) { throw 'Destination relay trust must exist before restoring a checkpoint.' }
$vm = Get-VM -Id ([Guid]$binding.vmId)
$snapshots = @(Get-VMSnapshot -VM $vm)
$candidates = @($snapshots | Where-Object {$_.Name -like 'permissive-healthy-*'} | Sort-Object CreationTime -Descending)
$candidates += @($snapshots | Where-Object {$_.Name -eq 'permissive-baseline'} | Sort-Object CreationTime -Descending)
if (!$candidates.Count) { throw 'No healthy checkpoint or baseline is available.' }
foreach ($snapshot in $candidates) {
    try {
        $vm | Get-VMNetworkAdapter | Disconnect-VMNetworkAdapter
        if ((Get-VM -Id $vm.Id).State -ne 'Off') { $vm | Stop-VM -TurnOff -Force }
        Restore-VMSnapshot -VMSnapshot $snapshot -Confirm:$false
        # Snapshot configuration can restore source-host NIC/ACL settings.
        # Never start the restored guest until those settings have been discarded.
        $vm = Get-VM -Id ([Guid]$binding.vmId)
        $vm | Get-VMNetworkAdapter | Disconnect-VMNetworkAdapter
        if ($vm.State -eq 'Saved') { $vm | Remove-VMSavedState }
        if ((Get-VM -Id $vm.Id).State -ne 'Off') { $vm | Stop-VM -TurnOff -Force }
        Set-VM -VM $vm -AutomaticStartAction Nothing -AutomaticCheckpointsEnabled $false -CheckpointType ProductionOnly
        Set-SandboxPerimeter $binding
        $vm | Start-VM
        & (Join-Path $PSScriptRoot 'Complete-Sandbox.ps1') -BindingPath $BindingPath -RelayCertificatePath $binding.relayTls.certificate -InitializationTimeoutSeconds 180 | Out-Null
        @{ok=$true;checkpoint=$snapshot.Name} | ConvertTo-Json -Compress
        return
    } catch {
        $vm | Get-VMNetworkAdapter | Disconnect-VMNetworkAdapter
        if ((Get-VM -Id $vm.Id).State -ne 'Off') { $vm | Stop-VM -TurnOff -Force }
    }
}
throw 'All known checkpoint bindings failed readiness. Guest remains off and disconnected.'
