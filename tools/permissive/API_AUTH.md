# Public/private key API authentication

## Android companion signatures

The optional Android companion has a separate listener and authentication
profile. It does not change the Ed25519 API described below. Tailscale Serve
exposes `https://HOST.TAILNET.ts.net:9444` to a loopback-only companion on 8790.
The companion accepts only enrolled P-256 phone signatures, then signs permitted
requests to the existing Windows service using a dedicated DPAPI-protected Ed25519
client. No Windows private key is transferred to Android.

The default gateway requires a bound Windows controller. A client-only upgrade
can explicitly select its existing signed service at `http://127.0.0.1:8789`.
This local opt-in accepts the older authenticated health response without an
execution role; it rejects explicit guest, unknown, or mismatched roles. It does
not replace or start the execution backend.

The local Windows pairing panel creates a random 256-bit, one-use invitation
valid for five minutes. Its QR is a JSON object with `version: 1`, `origin`,
`installationId`, `invitationId`, `secret`, and `expiresAt` (Unix seconds).
`POST /mobile/v1/enroll` carries `invitationId`, `secret`, `publicKey` (base64
canonical P-256 SPKI DER), and `deviceName`, and signs the request using that
public key. A consumed invitation can be retried before expiry by the same key,
but cannot restore a revoked device. All normal requests require enrollment.
Signed `GET /mobile/v1/identity` returns installation/device identity, origin,
protocol version, and capabilities.

Use RFC 9421 label `permissive-mobile`, algorithm `ecdsa-p256-sha256`, and these
components in order:

```text
"@method" "@authority" "@path" "@query" "content-digest" "permissive-instance-id"
```

Append `"content-type"`, `"idempotency-key"`, and `"last-event-id"` when present,
in that order. `Permissive-Instance-Id` must match the paired installation UUID;
authority is the configured lowercase HTTPS hostname with `:9444`. Key ID is
lowercase SHA-256 hex of canonical public SPKI DER. Parameters retain the strict
ordering, nonce rules, 60-second default/120-second maximum validity, and
30-second future allowance described below, using the mobile label/algorithm.
The signature is base64 of 64 raw bytes `r[32] || s[32]`; Android's JCA DER ECDSA
output must be converted before sending. Hash the signature base exactly once.

Nonces persist across companion restarts. New connections need fresh signatures;
retry submissions with their original idempotency key. Admitted event streams
can outlive signature expiry. Local phone revocation stops new requests and
closes admitted streams within 15 seconds, leaving Windows tasks running.

Only existing-workspace reads, conversation/task activity and chat controls,
attachments, artifacts, the remote browser/profile catalog (`GET /v1/browsers`),
and secure chat/workspace deletion are available through this listener.
`POST /v1/chats/{id}/secure-delete` and `POST /v1/workspaces/{id}/secure-delete`
require exactly `{"confirm":"ID","passes":7}`, with `confirm` matching the target
ID. The app supplies this body after its deletion confirmation; typing a keyword
is not required. The service checks that tasks are idle and performs the deletion.
Workspace creation/editing, host setup, shutdown, direct browser commands,
browser setup/connect, and credential management are excluded. Requests carrying browser Origin or Funnel
headers are rejected. There is no email-owner check: enrollment authorizes the
phone, while Tailscale restricts connectivity.

Enrollment, invitations, replay data, and installation UUID live under the
instance's `companion` directory, excluded from portable exports. The companion
backend key is also excluded. A portable import requires fresh pairing.

## Existing desktop, CLI, and automation clients

All Codex Permissive API requests require an Ed25519 signature. Clients retain
their private keys. Install only their public keys into the instance. The
service never accepts a public key as a credential by itself and never accepts
legacy bearer tokens. HTTPS remains the transport for remote clients.

## Provision a client

Run on the **client system**, using its installed Codex Permissive CLI:

```powershell
codex-permissive.exe auth generate --private-key C:/client/permissive-private.pem --public-key C:/client/permissive-public.pub
```

Alternatively, use `ssh-keygen -t ed25519` with a dedicated key. PEM and OpenSSH
Ed25519 keys are supported. For an encrypted private key, supply its passphrase
through `PERMISSIVE_KEY_PASSWORD`. Protect the private file on its client system.

Copy **only the public file** to the execution host, then install it locally:

```powershell
codex-permissive.exe auth add C:/keys/permissive-public.pub --name "Official Codex delegation"
codex-permissive.exe auth list
codex-permissive.exe auth revoke KEY_ID
```

The registry is `%LOCALAPPDATA%/CodexPermissive/auth/authorized-keys.json`.
Key IDs are lowercase SHA-256 hex fingerprints of the raw 32-byte public key.
Adding or revoking a key takes effect on the next request without restarting the
service. Revocation blocks new requests; already admitted work/connections may
finish. No key-management endpoint is exposed over the network.

Normal client keys have the `api` scope. The native browser host and attached
browser worker use a separate `browser` key, valid only for `/v1/browser/*`.
`auth add --scope browser` provisions that scope explicitly.

The installer provisions the local desktop/CLI and browser clients once. Their
private keys live outside the instance home, under
`%USERPROFILE%/.codex-permissive-clients/INSTANCE_HASH/{api,browser}/private-key.dpapi`.
This location is shared by Store-packaged Codex and normal Chrome/Edge processes;
their AppData filesystem views can differ. Local provisioning migrates existing
encrypted keys from the old AppData location without changing their identities,
restoring revoked authorization, or deleting files used by running clients.
They are encrypted with the signed-in Windows user's DPAPI. Starting the service
or desktop again does not restore revoked keys. `auth init-local` is the explicit
local-owner command for restoring these clients; do not run it for remote clients.

## Delegate from another system

Use `--private-key PATH` before a CLI command, or set `PERMISSIVE_PRIVATE_KEY`:

```powershell
codex-permissive.exe --url https://pshyperv.taileef59.ts.net:9443 --private-key C:/client/permissive-private.pem workspaces
codex-permissive.exe --url https://pshyperv.taileef59.ts.net:9443 --private-key C:/client/permissive-private.pem exec --workspace WORKSPACE_ID "Implement and verify the change"
```

For MCP delegation, configure the same URL/private-key arguments before `mcp`.
On this host, the official Codex adapter defaults to its protected local API
client key. Reconnect an existing adapter after installing this update.

Python clients can use `permissive.client.SignedSession` from `tools/permissive`
(install `requirements.txt` and put that directory on `PYTHONPATH`):

```python
import asyncio
from permissive.client import SignedSession

async def delegate():
    base = "https://pshyperv.taileef59.ts.net:9443"
    async with SignedSession(private_key="permissive-private.pem") as client:
        async with client.post(base + "/v1/tasks", json={
            "workspaceId": "WORKSPACE_ID", "prompt": "Implement and verify the change"
        }, headers={"Idempotency-Key": "unique-delegation-id"}) as response:
            response.raise_for_status()
            task = await response.json()
        print(task["id"])
        async with client.get(base + "/v1/tasks/" + task["id"] + "/events") as response:
            response.raise_for_status()
            async for line in response.content:
                print(line.decode(), end="")

asyncio.run(delegate())
```

Each retry/reconnection must have a fresh nonce/signature; keep the same
`Idempotency-Key` for retries of the same submission. Signatures authorize the
request when it arrives. Their expiry does **not** impose a model, task, SSE, or
WebSocket lifetime; long Sparky processing and admitted streams remain supported.

## HTTP signing profile

The application uses a strict [RFC 9421 HTTP Message Signatures](https://www.rfc-editor.org/rfc/rfc9421.html)
profile with the label `permissive` and algorithm `ed25519`:

* Required components, in order: `"@method" "@path" "@query" "content-digest"`.
* Then, when present, in order: `"content-type" "idempotency-key" "last-event-id"`.
* Digest: `Content-Digest: sha-256=:BASE64_SHA256_OF_EXACT_BODY_BYTES:`.
  Empty bodies also require their digest. Sign any content-type added by the
  HTTP library, including on an empty POST.
* `Signature-Input: permissive=(COMPONENTS);created=UNIX_SECONDS;expires=UNIX_SECONDS;nonce="NONCE";keyid="FINGERPRINT";alg="ed25519"`.
  Parameter order/spacing must match this profile exactly.
* `Signature: permissive=:BASE64_64_BYTE_ED25519_SIGNATURE:`.
* Default validity is 60 seconds; maximum is 120 seconds. Creation may be at most
  30 seconds in the future. Nonces are 22–64 characters of base64url text.
* Duplicate signature/control headers, absent coverage, changed body/target,
  expired/unknown/revoked keys, wrong scopes, and replayed signatures are rejected.
  Nonce records persist in `auth/replay.sqlite` across service restarts.

The signature base is newline-separated ASCII, without a trailing newline:

```text
"@method": POST
"@path": /v1/tasks
"@query": ?
"content-digest": sha-256=:BODY_DIGEST:
"content-type": application/json
"@signature-params": ("@method" "@path" "@query" "content-digest" "content-type");created=CREATED;expires=EXPIRES;nonce="NONCE";keyid="FINGERPRINT";alg="ed25519"
```

Use the exact encoded path/query sent on the wire. `@query` includes its leading
`?`, even when absent. No redirects are followed by the bundled signed clients;
a changed target needs a new signature. The URL authority is not signed, allowing
the same instance to be reached through its loopback and Tailscale addresses.

For non-Python integrations, `python -m permissive.auth --home INSTANCE_HOME sign
--private-key CLIENT_KEY` reads a JSON object from stdin containing `method`,
`target`, a base64-encoded `body`, and optional `headers`, and returns signed
headers as JSON. It never emits private key material. See `client/signing.cjs`
for the Node integration. Sign once per request; do not cache signed headers.
