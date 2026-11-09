# Autonomous Windows sandbox

Run the host helpers from **elevated Windows PowerShell 5.1**. They do not
silently elevate, change unrelated VMs, or fall back to executing model commands
on the host. The guest runs the agent and native desktop helper as its dedicated
local administrator. Host code owns Hyper-V, transport, the inference relay, and
the network perimeter.

Use the same Windows administrator account for provisioning, the registered
controller, and the local desktop. A standard-user session needs administrative
credentials; approving elevation as another account does not transfer that
account's DPAPI identities or interactive logon to the original user. Remote
API clients can use separately authorized signing keys.

Completion registers `CodexPermissiveController-<sandboxId>` for the current
host user at highest privilege, using its interactive logon and DPAPI identity.
The ordinary desktop starts this fixed task and checks its controller role and
VM ID before connecting. The host user must be logged on. No host password is
stored by Task Scheduler. An older host-execution service on the same API port
must be stopped before this controller starts.

## Provision

Build the standalone package first. `windows/package.ps1` compiles
`native/desktop-agent.exe` and includes this toolkit. Obtain an x64 Windows 11
multi-edition ISO and its SHA-256 from the [official Microsoft download
page](https://www.microsoft.com/software-download/windows11). A local file must
match the independently supplied official hash; `-IsoUrl` additionally requires
a Microsoft HTTPS hostname. No unsigned third-party Windows image is used.

```powershell
$package = 'C:\Apps\CodexPermissive'
$officialHash = Read-Host 'Official Windows 11 ISO SHA-256'
& "$package\windows\sandbox\New-Sandbox.ps1" `
    -PackageDirectory $package -IsoPath 'D:\Media\Windows11.iso' `
    -IsoSha256 $officialHash
```

The helper applies the ISO's Windows 11 Pro image to a new 160 GiB dynamic VHDX,
then creates a Generation 2 VM with configuration version 9.0, eight vCPUs,
16 GiB memory, Secure Boot off, no vTPM, and no BitLocker requirement. Windows is
deployed offline; its first boot has a disconnected network adapter. Activation
and Windows servicing remain normal guest OS operations. Applying Windows 11
without its TPM/Secure Boot installation requirements is not a Microsoft
supported hardware configuration.

The generated `SandboxAdmin` credential is retained in a host-user DPAPI
`guest-credential.xml` envelope inside an administrator-restricted directory.
Windows setup transiently needs the guest-only password in its unattend file;
initialization removes known cached answer files, removes registry
`DefaultPassword`, and stores autologon data as an LSA private secret. This is
not cryptographic erasure of an unencrypted VHDX. No host administrator password,
Tailscale identity, relay private key, or inference credential enters the guest.

The host binding is written beneath
`%ProgramData%\CodexPermissive\Sandboxes\<id>\deployment.json`. A copy becomes
the installed package's `deployment.json`. `controllerHome` records the actual
host history directory; specify `-ControllerHome` when using another directory.
`-ControllerPort` selects its loopback API port (8789 by default); the packaged
desktop and CLI read that binding. A separate port and home allow acceptance
while an older installation still has active work.
The guest receives a separate deployment file with `role: guest`, the fixed
gateway alias, and its public relay certificate. The host inference endpoint is
configurable with `-UpstreamEndpoint` and is absent from portable bundle metadata.

TLS is initialized by `permissive.inference_relay --initialize-tls`.
`Complete-Sandbox.ps1 -BindingPath ... -RelayCertificatePath ...` initializes the
guest and attaches its adapter only after verifying the host perimeter. Failure
leaves it disconnected. Use `New-Sandbox.ps1 -Resume` with the original VM name
to complete a provisioning attempt that already produced its host binding.

## Isolation and management

Each guest has one adapter and its own internal vSwitch and `/30`. A project-owned
WinNAT provides IPv4 TCP/UDP Internet egress. A non-overlapping `/24` is selected
from `172.16.0.0/12`; project guests get separate `/30`s beneath it. An unrelated
WinNAT causes a preflight error; the toolkit never deletes or widens another NAT.
The host's observed public IPv4 is discovered with `api.ipify.org` or supplied
with `-PublicEgressAddress` to block ordinary NAT hairpin access.

Host vNIC ACLs deny private, CGNAT/Tailscale, link-local/metadata, loopback,
multicast, reserved, host, and connected-network addresses. The sole private
exception is stateful TCP to that guest's gateway on port 18445. The relay has
one fixed inference upstream. IPv6 is blocked at the vSwitch, guest source IPv4
is pinned in every allow rule, and MAC spoofing is disabled. DHCP and router
guards are enabled. Unsolicited inbound traffic and incoming NAT mappings are
disallowed. These settings are host-owned; guest administrator changes to its
firewall do not weaken them.

Management uses PowerShell Direct over VMBus, without a host administration
network API. `Invoke-SandboxCommand.ps1` accepts a host-owned JSON request file
and passes its contents as data to the fixed guest bridge over stdin, avoiding
Windows command-line size limits and shell evaluation. The guest's
`CodexPermissiveGuest` task runs at highest privilege with an interactive logon
token; its home is `C:\ProgramData\CodexPermissiveGuest`. An interactive token
is required for desktop control. The agent can change guest OS settings and
recover its own desktop; host Hyper-V controls remain outside its world.

Internet access permits public services and their proxy behavior; this perimeter
restricts direct network destinations. Revalidate/rebind after changing host
routes, subnets, public IP, or restoring a checkpoint. No shares, mapped host
drives, host credentials, or enhanced-session drive/clipboard redirection are
configured by these helpers.

Host collection budgets default to 256 MiB/100,000 journal events per task,
8 MiB per event/status, 2 GiB per artifact, and 8 GiB/256 retained artifacts per
task. Transport output is capped at 64 MiB and host PowerShell helpers at 1 GiB
memory. A protocol/budget failure stops and restores the guest before freeing
its slot; failure to confirm shutdown leaves it quarantined until a reset.
These are limits on data entering the host, not guest command restrictions.

The host controller also checks free space on the VM storage and controller
history volumes before starting/resuming the guest and every two seconds,
including while no task is running. The host binding's `minHostFreeBytes`
defaults to 64 GiB. Crossing that reserve stops the VM, persists a storage
quarantine, and holds an active task slot until Hyper-V confirms shutdown.
Failed shutdowns are retried. Freeing space alone does not resume the guest;
use the environment reset control after both volumes have sufficient space.
Controller shutdown pauses the VM even when it has no active tasks.

Checkpoints additionally require free VM storage equal to that reserve plus
twice `maxDiskBytes` (160 GiB by default), allowing for a differencing-disk merge
and further guest disk growth. Insufficient checkpoint headroom skips the
checkpoint and records `checkpoint_error` while preserving completed work and
the existing recovery points. These budgets are host binding settings; guest
commands and guest API data cannot alter them.

## Move to another host

First use the controller's signed `/v1/environment/export` API (or the matching
application export command) to fence new work and pause execution. The helper
checks an active controller's fence before exporting. Export shuts the guest
down, detaches its vNIC in the exported configuration, and restores the source
binding while leaving the source VM off and fenced.

```powershell
& "$package\windows\sandbox\Export-Sandbox.ps1" `
    -BindingPath "$package\deployment.json" -OutputPath 'E:\Transfer\Sandbox'
# Copy the complete Sandbox directory to the destination host.
& "$package\windows\sandbox\Import-Sandbox.ps1" `
    -BundlePath 'E:\Transfer\Sandbox' -PackageDirectory $package
```

Both helpers prompt securely for the same **transport passphrase**. This protects
the small portable guest-credential envelope with scrypt and authenticated
AES-GCM; it does not encrypt the VM disk or controller history. Scripts can
receive an existing `SecureString` through `-TransportPassword`; an API process
can use `-TransportPasswordStdin` and a private stdin pipe. A passphrase is never
put in command-line arguments, environment variables, or diagnostic logs.

The bundle contains the powered-off VM configuration and disks/checkpoints,
toolkit, controller snapshot, encrypted guest credential, and file hashes.
Source host bindings, DPAPI client private keys, relay private keys, upstream
credentials, original gateway addresses, and guardian certificates are excluded.
Hashes detect transfer damage; treat a bundle as a trusted VM image, not as
independently authenticated software.

Import validates hashes and configuration compatibility, copies with a fresh
Hyper-V VM ID, reissues host DPAPI/TLS material, creates destination networking,
and rebinds the guest before connecting it. The destination controller history
directory must be empty; existing history is never overwritten. Specify
`-RootPath`, `-ControllerHome`, `-VMName`, or `-UpstreamEndpoint` as needed.
The original source VM is retained off; deleting it is a separate user action.
Only compatible Windows x64 Hyper-V hosts are supported; this is a cold move,
not a saved-state/live migration.

## Validation

With the controller running and the guest idle, invoke the fixed live boundary
checks, then the signed DeepSeek desktop acceptance:

```powershell
& "$package\windows\sandbox\Test-SandboxIsolation.ps1" -BindingPath "$package\deployment.json"
# Run from the source checkout with its service dependencies available.
python tools/permissive/tests/sandbox_acceptance.py --deployment "$package\deployment.json"
```

The isolation helper temporarily disables the guest firewall and creates its
own host listeners, then restores guest settings and removes its temporary
host resources. It records untested cases explicitly. The desktop helper
requires actual native tool calls, Calculator's displayed 703, exact UTF-8 file
bytes, PNG artifacts, and independent guest hostname/model rollout evidence.

Automated tests parse every script with the Windows PowerShell parser and verify
the generated ACL decision table without creating a VM. Actual acceptance needs
an elevated compatible host: provision from the pinned ISO, verify guest health
and native desktop interaction, disable the guest firewall and attempt LAN,
host, metadata and Tailscale access, change its source IP/MAC and enable IPv6,
and verify those attempts remain denied. Public HTTPS/DNS and streaming model
inference must work. Export/import to a second host must retain history, reissue
host credentials/TLS, and pass the same boundary checks after restart. Do not
report those integration checks as passed merely because parsing/unit tests pass.
