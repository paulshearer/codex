# Codex Permissive

## Tool harness

The Sparky worker exposes code mode only. Provider-facing custom tools describe
one JSON envelope with a string `input`; nested JavaScript tools retain their
own argument types. Each request receives current task capability context.
Attached-browser preflight reads the selected live extension inventory without
opening tabs. Missing desktop helper files omit that tool and report the
installation problem; the desktop status operation reports session/input limits.

The task-local HTTP MCP provides typed JSON requests, explicit 0.1–120 second
deadlines (30 seconds by default), bounded response bodies, and explicit TLS
verification. Redirects require a separate request. Cookies and recognized JSON
credential references are scoped to the exact origin and expire with the worker.
References can be reused in headers or JSON without exposing their values.
After a mutating request times out, observe state before deciding to retry.

Trusted native tool-contract failures receive two correction opportunities;
three consecutive failures stop with the saved progress. Unknown error text from
websites/documents does not trigger this policy. Repeated silent terminal polls
produce an inspection hint rather than imposing a universal command deadline.
Generation that has started but emits no meaningful delta for 180 seconds fails
with a distinct recoverable error; heartbeats do not count as progress. Initial
prefill and legitimate long-running tools have no new overall deadline. Existing
native stream recovery and rejected outer tool envelopes share a two-continuation
limit and preserve completed actions.

`tests/test_harness_policy.py`, `tests/test_http_worker.py`, and
`../custom-models/test_generation_progress.py` cover these boundaries.
`tests/harness_device_acceptance.py --native PATH --output DIR` runs the real
Sparky model with automatic tool selection against local device and browser
fixtures. It checks version comparison, one-time setup, existing login,
self-signed TLS, timeout fallback, and connected/disconnected browser paths;
it never contacts a real appliance. Its isolated native profile and full event
evidence are retained in the output directory. Package verification requires both
HTTP modules and the native desktop helper before accepting an installation.

See the [padded/asymmetric regression report](HARNESS_REGRESSION.md) for the
matching scenarios, deployed verification and scope of these checks.

Chats use a stable UUID and separate `chat-segments/<UUID>-1.sqlite`, `-2.sqlite`,
and later files at compaction boundaries. The highest number is current. Each
file contains its own transcript, attachment references and compaction checkpoint;
the shared task database contains routing and task metadata. Opening a chat reads
only its latest file. **Load previous segment** at the compaction boundary prepends
one earlier file and retains the reading position. Expanded history stays open
during refreshes; reopening a chat starts with the latest segment.

Ordinary continuations reuse the native conversation after compaction. Retained
history is imported only when edits or a changed execution environment require
it. Routine restoration details stay in Activity; active retry notices disappear
when output resumes. Reply-version snapshots read and cache points for the
selected completed tasks instead of rescanning whole segment transcripts.

Message actions carry a segment ID. Regeneration reads only that segment and its
saved context from before the response. Earlier immutable files can be shared by
branches; secure deletion erases them only after their last reference is removed.
Existing chats migrate off the API thread after active/paused work finishes.
Migration validates files before switching pointers and reconstructs a missing
native checkpoint once with tools disabled. Failed migrations retain the original
data. Publication and background deletion recover after a service restart.

Each desktop and Android prompt and reply has a visible **⋮ Message actions**
button at its top right, including saved replies shown while history loads.
The menu starts with **Edit**, **Delete**, and **Regenerate**, followed by **Copy**,
**Fork from here**, and **Delete from here**. Editing changes a
completed prompt or reply in place, preserves later messages and attachments,
and uses the revised conversation in future turns. Failed saves retain the edit
for retry. Deletion preserves the reading position, clamped to the retained chat.
**Delete** removes only the selected message and keeps later messages. **Regenerate**
immediately resubmits the selected turn's original prompt and attachments. It saves
another response version in the same logical chat, preserving the old response and
its later conversation. **‹ Reply 2 of 3 ›** switches versions on Windows and Android.
Each device remembers its own selected branch. Generation starts before transcript
and artifact copying; durable background preparation resumes after restart without
reexecuting the prompt. Switching and further regeneration wait for generation and
preparation to finish; cancellation remains available. Commands may run again.
Whole-chat deletion removes all response versions. Message edits and deletions
affect the selected branch and retain sibling versions.
Delete and Delete from here open an independent replacement chat
with the original title and the requested changes. The original disappears from
the chat list immediately. A durable background queue securely erases it in seven
passes, retrying storage or guest failures and resuming after service restarts.
Native database preparation
waits for idle model processes; detached files are overwritten off the API event
loop, so users can continue in the replacement chat during erasure.
Done, Stats, Activity and Attachments share a compact row with details on demand.
Fork includes the selected completed
message and all earlier messages in a new chat named “— Fork”, then opens that
chat. The original stays available. Attachments and retained artifacts are copied
independently. New model turns import the retained messages, attachments, and tool
evidence into a fresh native thread, without later messages or compacted context.
Tool evidence uses bounded excerpts for model context; the complete retained
records remain available in the chat timeline. Windows database cleanup retries
brief sharing violations before reporting a deletion error.
Large edited conversations restore recent messages and excerpts first, with the
complete retained prefix available to the model in a task-owned local archive.
This avoids expanding a previously compacted conversation into a long initial
model request. Archives are removed with the conversation's secure cleanup.

**Delete from here** asks for confirmation, then removes that message and all
later messages and queued follow-ups. Earlier messages remain; deleting the first
prompt leaves an empty chat you can continue. Saved app data uses the existing
seven-pass cleanup. Deletion and forking do not undo commands, file changes, or
external actions. Finish or cancel the selected chat before either operation.
Other chats can keep running while the replacement is created. Whole-chat deletion remains
available in the chat list and Android chat actions.

Signed clients can call `POST /v1/chats/{id}/fork` or `/delete-from` with
`taskId`, `messageId`, and a UUID `submissionId`. A message ID is `prompt`,
`result` for a legacy result without native message events, or the string ID of
the message's first native/steering event. Deletion additionally requires
`confirm` equal to the chat ID. Both return a chat snapshot. Retry with the same
submission ID after an uncertain response to retrieve the same replacement,
including after the original has been erased. Use the returned chat ID for all
subsequent requests and event replay. `cleanupPending` reports queued cleanup;
health reports `pendingHistoryDeletions`. Superseded chats and tasks return 410
while cleanup is pending, and are excluded from lists.

`POST /v1/chats/{id}/edit-message` accepts `taskId`, `messageId`, a UUID
`submissionId`, and nonblank `text` up to 1 MiB, and returns the revised snapshot.
Reuse the submission ID only when retrying the same edit. Finish or cancel running
tasks before editing. Edits retain later messages and do not rerun earlier actions.

`POST /v1/chats/{id}/delete-message` removes only the selected message.
`POST /v1/chats/{id}/regenerate` creates another response version. Both take the same
message identifiers and submission UUID as `delete-from`; only deletion requires
`confirm` (regeneration accepts it for older clients). Snapshots expose `chatGroupId`,
`replyVersions`, `branchPreparing`, `versionsBusy`, and `branchPreparationError`.
Open a version's `conversationId` through the existing snapshot and event APIs,
clearing the previous branch's replay buffers. Chat lists group physical branches
into one logical chat. Regeneration retries return the same task, even after a
restart. The `response` message ID addresses a task's complete
saved reply, including combined native messages, before the client finishes replay.

## Android companion

Android v0.1.3 displays saved replies before history catch-up and restarts replay
when deletion replaces task IDs. Its initial replay uses
`GET /v1/chats/{id}/events?format=json&view=mobile&through=CURSOR` and follows
`nextCursor`. Historical text deltas are coalesced without changing message IDs;
timing samples are summarized. Live SSE remains unchanged. Each reconnect rebuilds
the compact snapshot before resuming live events, so partial replies lose no text.

Rename a chat with its pencil button or **Rename chat** in the conversation header.
Android offers **Rename chat** in each drawer chat's actions menu and under
**More actions**. Names are shared across both clients and persist through
continuations and restarts. Titles accept a single line of 1–120 characters.
The signed API accepts `PATCH /v1/chats/{id}` with `{"title":"New name"}`.

Android also offers **Copy chat ID** in those menus. It copies the native Codex
thread ID, matching the existing Windows chat context menu. A loaded ID can be
copied while offline; the action becomes available once the task has started.

The native **Permissive Codex** Android app shares this Windows controller's
workspaces, conversations, task controls, attachments, and live activity. Each
device can select a different chat. Windows retains history and executes work;
Android persists encrypted unsent drafts and connection settings only.

Build the Android release with `android/tools/build-release.ps1` from the
repository root. The app requires Android 8 or newer and the installed Tailscale
app configured for your tailnet. In the updated Windows desktop, choose **Pair
Android → Create pairing QR**, then scan the QR or paste its pairing code in
Android. The invitation expires after five minutes and enrolls one phone key.
Use the same Windows panel to revoke a phone or disable companion access.

Enrollment authenticates the phone's protected Android Keystore key. Tailscale
provides the private connection; the app does not require a particular Tailscale
account email. Each enrolled phone can access the existing workspaces on its
paired installation. The companion accepts no public-network fallback, account
password, bearer token, or phone-side model credentials.

The user-session companion task survives closing the desktop. Windows must stay
awake, signed in, and connected to Tailscale, with its existing Windows service
running. Updating an existing Windows client does not require provisioning a VM
or moving its history and project folders. A client-only installation selects
the existing signed loopback service with a local `client-connection.json`:
`{"mode":"existing-service","origin":"http://127.0.0.1:8789"}`. The desktop
does not start or replace a backend in this mode. Deployments with a configured
Hyper-V controller retain their bound-controller checks.
The companion starts at Windows sign-in and checks once per minute for a stopped
process, including when the backend was unavailable during startup. Running
instances are left alone; disabling companion access also disables its task.

Existing installations that use the pre-VM backend can repair their startup with
`windows/register-existing-service-autostart.ps1 -PackageDirectory BACKEND -HomeDirectory HISTORY`.
This registers the installed backend as a foreground scheduled task with sign-in
and recurring recovery triggers, resolves the existing physical history directory,
and replaces the old registry startup entry after backing it up. It uses the
installed runtime directly, without a dependency on a temporary build directory.
The backend and companion run as the signed-in Windows user so their protected
client keys remain available. Bound VM deployments use their controller task.
The companion uses HTTPS port **9444** through Tailscale Serve and loopback port
**8790**, separately from the existing Ed25519 API on **9443/8789**. Setup refuses
occupied ports belonging to another application. New portable imports require
fresh pairing; ordinary upgrades preserve enrollment.

Local management is available through `windows/mobile-companion.ps1` with
`-Action Status`, `Pair`, `Revoke -DeviceId ID`, or `Disable`, and the package/data
directory parameters. Device and invitation management are never network routes.
See [the mobile signing contract](API_AUTH.md#android-companion-signatures) and
the repository's [Android instructions](../../android/README.md).

Paste an image into the message box with **Ctrl+V**, or use **+ → Attach files…** to select images, documents, and other files. Draft attachments show thumbnails or filenames and can be removed before sending. You can send attachments without text, with a continuation, while steering an active turn, or in a queued follow-up. Images enter the model's native vision input; other files are copied into private managed storage and exposed to the agent's file-reading tools. Submitted attachments stay visible in the chat and can be saved through a native save dialog. Limits are 10 files per message, 16 MiB per file, and 32 MiB combined. Continuations do not automatically reattach the previous message's files. Failed submissions retain the attachment draft for retry.

Attachment uploads use signed `POST /v1/attachments` requests containing `workspaceId`, `name`, and base64 `data`; include the returned IDs in the message's `attachments` array. Metadata and previews are available at `GET /v1/attachments/{id}`, and original bytes at `/content`. `DELETE /v1/attachments/{id}` overwrites unsent copies seven times. Sent copies belong to one chat and are included in its secure deletion. Workspace deletion also erases its unattached drafts. Your original files and separately saved exports remain intact.

Independent Windows desktop, CLI, and task API using only the local DeepSeek V4.1 Flash model on Sparky. The native agent is based on Codex `rust-v0.162.1` (`092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`). The official Codex desktop is not required.

The desktop shell uses Rust and Tauri, with the existing React interface embedded in the executable. Rust handles signed API requests, event streams, file dialogs, clipboard actions, and local service startup. The Python task service and Rust native agent retain the existing workspace and conversation data. Windows WebView2 renders the interface; Node and Chromium are used separately for browser automation.

The host desktop and authenticated controller keep history, dispatch work, and manage Hyper-V. Commands, file edits, native desktop control, and browser automation run as the dedicated guest administrator inside a Windows VM, without harness action-review prompts or command-name bans. The VM provides the isolation boundary. Host vSwitch ACLs block direct host, LAN, tailnet, metadata, and IPv6 access; public IPv4 Internet access and one fixed TLS inference relay are available. One task controls the guest at a time. There is no host execution fallback.

Build the package, then follow [sandbox provisioning and portable export/import](windows/sandbox/README.md). The default guest is Windows 11 Pro, Generation 2/configuration version 9.0, with eight vCPUs, 16 GiB RAM, and a 160 GiB dynamic VHDX. It has no BitLocker, Secure Boot, or vTPM dependency. Provisioning verifies an official Microsoft ISO against its published SHA-256. The guest autologon account runs the agent elevated and disables idle sleep/lock. Host API authentication and Hyper-V management credentials remain outside the guest.

## Build and launch

```powershell
./tools/permissive/windows/build.ps1 -BuildDirectory C:/codex-build/standalone-0.162.1
cd tools/permissive
npm ci
npx playwright install chromium
npm run build-ui
cd ../..
./tools/permissive/windows/build-desktop.ps1
./tools/permissive/windows/package.ps1
```

The package includes the Rust desktop, native executables and desktop helper, Python and dependencies, Node, PowerShell 7.6.6, Chromium, and the sandbox toolkit. Windows must have the WebView2 runtime installed. Verify it with `windows/verify.ps1`. Provision the VM from the final package location before opening **CodexPermissive.exe**. Completion writes its controller `deployment.json` and registers an elevated controller task at user logon. The normal desktop starts that fixed task; model code never runs in it. Keep the package at that location, or rebind through the completion helper after moving it. The service remains running when the desktop window closes.

When upgrading an existing installation, let its running tasks finish and stop the previous service with `codex-permissive.exe stop` before running the installer. Closing the desktop window leaves that service running.

For development, `npm run desktop` builds a debug desktop and launches it with the packaged runtime. Set `PERMISSIVE_PACKAGE` to another verified package directory if needed. Package verification checks both native and desktop source hashes and requires the WebView2 crash-upload privacy patch. A fresh package directory is required when replacing an older Electron package.

Application data remains under `%LOCALAPPDATA%/CodexPermissive`; upgrading the desktop preserves existing tasks, workspaces, and chats. The new WebView2 profile lives in `desktop-webview` under that directory; unsent UI drafts and preferences from the previous desktop profile are not transferred. Existing official and custom profiles retain their current locations. API access requires Ed25519 request signatures. The instance stores authorized public keys; local desktop/browser client private keys are protected using Windows DPAPI in the separate `%USERPROFILE%/.codex-permissive-clients` directory. Treat API clients as authorized operators of this Windows account.

```powershell
./codex-permissive.exe exec --cwd C:/workspaces/project "Implement and test the requested change"
./codex-permissive.exe chat --cwd C:/workspaces/project
./codex-permissive.exe status TASK_ID
./codex-permissive.exe resume TASK_ID "Continue with the next change"
./codex-permissive.exe cancel TASK_ID
./codex-permissive.exe pause TASK_ID
./codex-permissive.exe unpause TASK_ID
./codex-permissive.exe stop
./codex-permissive.exe environment status
./codex-permissive.exe environment pause
./codex-permissive.exe environment resume
```

Working directories are absolute paths **inside the guest**. Selected host skill trees and explicit attachments are copied into the guest; host working directories are never mounted. Downloaded artifacts are saved through a dialog and are not opened through host file associations. Portable export fences the source controller, powers off the VM, snapshots host history, and bundles the guest disk/checkpoints. Import creates a new VM ID and regenerates destination TLS, DPAPI credentials, network bindings, and local client keys. The source remains off and fenced.

## Temporary website credentials

Open **+ → Secure credentials** on Android or the web interface. Enter a label,
an HTTPS origin for the website (for example
`https://login.example.com`), username and password. Choose **Session** or a whole
number of hours from **1–168**, then select **Provide securely**. Entries are
available to running Codex tasks in the selected workspace, including existing
tasks and later continuations. Related sign-in subdomains share an entry: a login
provided for `https://www.aa.com` can fill on `https://login.aa.com`. Matching uses
the bundled Public Suffix List, including private hosting boundaries, and requires
HTTPS and the same port. Separate registered websites and tenants (for example,
`alice.github.io` and `bob.github.io`) require separate entries. IPs, internal or
unknown suffixes, and public suffixes keep exact-origin scope. **Revoke** removes
an entry; **Revoke all in this workspace** also cancels
unfinished handoffs. List refreshes remove expired entries automatically.

Browser navigation follows **list → inspect → act on a returned ref → inspect**.
The attached browser and dedicated browser share resolution of open shadow DOM
and same-origin frame controls, accessible field names, input events and form
destinations. Replaced elements and navigated pages invalidate refs; inspect again
instead of guessing CSS. Covered clicks report the overlay blocker. Cross-origin
frames are listed separately and are not traversed by the page resolver. Use
secure fill for passwords; never inject probe inputs or bridge credentials through
scripts. The agent is instructed to report a blocker after two failed attempts.

If an extension-owned frame prevents debugger attachment to a web tab, structured
page actions use Chrome's `scripting` API in the main frame's isolated world.
This fallback retains shadow refs and secure-fill checks and does not access
other extensions. Clicks use DOM events and require a follow-up inspection to
confirm the result. Arbitrary evaluation, uploads and debugger screenshots remain
unavailable in this mode. Browser policy restrictions do not trigger a fallback.

`permissive/public_suffix_list.dat` is the unmodified Mozilla PSL snapshot (ICANN
and PRIVATE sections, MPL 2.0; version recorded in its header). Refresh from
https://publicsuffix.org/list/public_suffix_list.dat when shipping updates.
Runtime credential access performs no network suffix lookup. The backend checks
the current page and the selected input's form/submitter origins before releasing
a short-lived fill lease. The browser rechecks the exact current origin and form
destinations at the instant of filling, including controls inside shadow roots.

On Android, paste or type the credentials into **+ → Secure credentials**.
The username and password fields support Android's standard manual paste action
and opt out of Autofill. The app does not proactively read the clipboard.
Credential input fields disable saved view state and personalized IME learning.
The password is masked, and the dialog blocks screenshots and recent-app previews. These fields
are not composer drafts and are never written to the phone's encrypted draft
store; closing the dialog discards them.

Each handoff gets a fresh single-use RSA-2048 key pair, valid for two minutes.
The client generates an independent AES-256-GCM key and 96-bit nonce, encrypts the
credential payload, and wraps that key with RSA-OAEP (SHA-256 and MGF1-SHA-256).
The UTF-8 additional authenticated data is `uploadId + "\n" + workspaceId`.
Only the encrypted envelope crosses desktop IPC or the phone gateway. The host
desktop disables WebView2 password autosave and general form autofill. The controller
validates its shape before staging anything through Hyper-V, and forwards it
unchanged to the execution service. The guest destroys the upload private key
after use and re-encrypts accepted username/password values under another fresh
per-entry AES key, held only in memory. There is no saved master key, persistent
secret database, environment-variable injection, or plaintext credential file. Expiry uses a
monotonic deadline and a background reaper; every access checks it again.

**Session** means the lifetime of the execution service, ending earlier on
revocation. Closing a client or finishing a model turn does not end it. Restarting
the execution service loses all entries, including timed entries with time left.
Website sessions/cookies are separate: revocation and expiry stop new credential
access, do not undo a fill already in progress, and do not sign a website out.

The browser MCP's **credentials** operation returns labels, origins, IDs and
expiry only. **fillCredential** takes an ID, `field` (`username` or `password`),
an inspected input `selector` and the selected page/tab. Its private signed
browser transport redeems the value in memory; the model-facing result is only
success or a fixed error. Both dedicated browsers and attached Chrome/Edge
profiles check the actual top-level HTTPS origin, input type/visibility, form
destination and a maximum 15-second fill lease immediately before changing the
DOM. Normal API and Android clients cannot call the browser redemption endpoint.
Codex instructions prohibit reading filled values back or saving passwords.

The execution service, browser, selected website and local administrator are
trusted to handle plaintext briefly while filling/submitting. Memory wiping in
managed runtimes is best effort; OS paging, hibernation, memory dumps and a
compromised guest/admin are outside this application's no-persistence guarantee.
Website output, screenshots, downloaded content and saved browser sessions can
contain information exposed by that website. Do not enter secrets into labels,
prompts or other ordinary saved fields.

The signed owner API exposes `POST /v1/credentials/prepare` (`workspaceId`),
`POST /v1/credentials` (sealed envelope), `GET /v1/credentials?workspaceId=ID`,
`DELETE /v1/credentials/{id}?workspaceId=ID` and
`DELETE /v1/credentials?workspaceId=ID`. Responses set `Cache-Control: no-store`.
The private browser-only `POST /v1/browser/credentials` is unavailable through
the phone gateway and PowerShell Direct relay. All components must be rebuilt
and deployed together before this feature is available in an installed version.

## Response recovery

Recognized premature Responses stream failures, including **Responses completed
event has no completed response**, trigger at most two continuation attempts after
one and two seconds. The task keeps its native conversation, completed tool
evidence and browser context. Recovery continues from recorded progress and asks
the agent to verify uncertain action outcomes before attempting them again.
The original prompt and attachments are not submitted again. Pause and Cancel
remain available. Authentication, quota, policy and unrelated failures stay failed.

If automatic recovery is exhausted, **Retry response** retries the latest failed
task from its saved conversation without sending the composer draft. Recovery
progress and original error details remain in the conversation. Held follow-up
queues remain held. Signed clients can use bodyless
`POST /v1/tasks/{id}/retry`; retry availability is returned as `canRetry`.

## Workspaces

Task elapsed time freezes at its persisted completion timestamp, including when reopening old chats. While a task runs, a light-gray **Thinking · live** area displays the model's streamed thinking text as chunks arrive, alongside progress summaries and tool activity. The text follows new output unless you scroll back. Each finished thinking step collapses to one sentence, preferring its model-provided summary and otherwise using a bounded first-sentence excerpt. **View streamed thinking** opens the saved text. Active thinking remains visible while subsequent chunks arrive. Existing chats reconstruct this view from their saved events. **TTFT** measures from the first inference request to its first streamed output, excluding queue and worker startup time. **Token Sec** shows a rolling two-second output rate, dropping to zero during waits. The provider stream exposes text rather than token IDs, so this rate is explicitly an estimate (four characters per token), including reasoning and tool arguments. **Compute time** totals wall time during command, file-change and local code-tool activity, counting overlapping activity once; it is elapsed activity time, not CPU utilization or model inference time. Completed tasks retain TTFT, their last streaming rate and compute time alongside `cachedInputTokens`, `inputTokens`, `outputTokens`, `reasoningOutputTokens`, and `totalTokens`, excluding prior conversation turns. Existing chats recover counters and timing from saved events when available; legacy event times have one-second precision.

**Attachments (N)** at the bottom right opens attached inputs and generated files across the current chat, including steering messages. Files stay out of the transcript and can be saved from the panel. The prominent status badge uses blue **Running (~N TPS)**, red **Failed**, and green **Done**. TPS is the same rolling two-second estimate shown in task statistics.

Drag the horizontal divider between Workspaces and Chats to change the workspace list height, and drag the vertical divider beside the sidebar to change its width. Both retain their sizes on reopening. Focus a divider and use arrow keys for ten-pixel adjustments, or Home/End for the minimum/maximum size.

Use **×** beside a chat or workspace for **Secure deletion**, then select **Delete permanently · 7 passes** in the dialog. Workspace deletion includes all its chats. Finish or cancel active tasks first: shared database maintenance requires an idle service and completed/cancelled chats. The operation overwrites app-owned history databases, selected native rollouts and shell snapshots, artifacts, controls, and affected diagnostic logs seven times with random data and flushes each pass. Shared databases and logs are rebuilt to preserve other chats. Interrupted operations retain a content-free retry receipt. Chat option drafts are removed from this desktop's local storage. Working directories, exports, browser profiles and system/desktop caches are outside the file-overwrite operation. Seven logical file overwrites cannot guarantee physical erasure on SSDs or erase snapshots/backups.

Signed API clients can use `POST /v1/chats/{id}/secure-delete` or `POST /v1/workspaces/{id}/secure-delete` with `{"confirm":"ID","passes":7}`. A running service task returns 409; active/paused chats must be finished or cancelled before retrying. Errors can indicate partially completed removal; retry the same endpoint to finish outstanding overwrites.

Assistant responses render GitHub-flavored Markdown. Tables follow EIF: a table card with row/column counts opens a scrollable table with sticky headers, alternating rows, alignment, and **Export as Excel**. Excel export uses a native save dialog and preserves text and numeric cells.

The sidebar separates **Workspaces** and their **Chats**, following the EIF layout. Create a workspace with **＋** beside Workspaces and edit it with its settings button. Each workspace has a name, working directory, browser mode, browser, browser profile, system prompt, and selected skills. Choose skills from the personal/project catalog or use **Add skill files…** to select other `SKILL.md` documents. Supporting files remain in each skill's original directory.

The picker shows the selected count and supports clearing the selection. Category buttons explicitly show how many skills they select and cannot exceed the workspace's 32-skill limit. Identical copied skill packages appear once in the catalog; different supporting files and explicitly assigned paths remain available. Repeated paths count once, and validation errors report the actual selected count.

Open **Skills** in the sidebar to search the library, view full skill instructions, set categories, select an entire category, and assign selected skills to the current workspace. Categories are shared across workspaces and saved separately from skill documents. **Add skill files…** registers existing documents in place. **Export selected skills…** saves a portable ZIP containing each skill's directory, supporting files, and category. **Import skills…** restores an exported bundle into fresh directories under the application's `skills` folder, preserving relative references and leaving existing skills intact. Bundles support up to 32 skills, 2048 files and 32 MiB; symbolic links are rejected.

Every task belongs to a workspace. The first launch after this update groups existing chats by their working directory while preserving task IDs, native chat IDs, events, results, and paused status. Legacy API and CLI requests specifying only a directory use an existing workspace for that directory or create one. Explicit workspace IDs distinguish multiple workspaces using the same directory.

Workspace prompts and the full text of assigned skills are captured when a task is submitted and provided as native developer instructions. Only assigned skills are injected into this context; the native automatic skill catalog is disabled. Settings changes affect new tasks and follow-up turns. Running and paused tasks retain their captured instructions, including after a service restart. Continuations remain in their original workspace and working directory. Up to 32 skills may be selected, with a 256 KiB limit per skill, 128 KiB for the system prompt, and 1 MiB for combined workspace context.

`GET/POST /v1/workspaces` list/create workspaces; `GET/PATCH /v1/workspaces/{id}` read/update settings. Creation requires `name` and `workingDirectory`; optional fields are `systemPrompt`, `skills` (absolute `SKILL.md` paths), `browserMode`, `browser` (`chromium`, `chrome`, `msedge`), and `browserProfile`. `GET /v1/skills?workspaceId=ID` returns available skill metadata including category. `GET /v1/skills/document?path=PATH` reads instructions; `POST /v1/skills/register` accepts `paths`, and `PATCH /v1/skills/categories` accepts `paths` and `category`. `POST /v1/skills/export` accepts `paths` and returns ZIP bytes; `POST /v1/skills/import` accepts an absolute `file` path on the execution host. `GET /v1/browsers` lists browser availability, named dedicated profiles and connected extension profiles. `POST /v1/tasks` accepts `workspaceId` and `prompt`, using workspace defaults when directory/browser are omitted. Filter saved chats with `GET /v1/tasks?workspaceId=ID`. Task results expose `workspaceId` and the captured `workspaceRevision`.

```powershell
./codex-permissive.exe workspaces
./codex-permissive.exe exec --workspace WORKSPACE_ID "Implement and verify this change"
./codex-permissive.exe chat --workspace WORKSPACE_ID
./codex-permissive.exe exec --workspace WORKSPACE_ID --browser dedicated --browser-type chrome --browser-profile work "Inspect the application"
```

MCP exposes `workspaces` and `save_workspace` alongside the existing task tools. `submit` accepts a workspace ID in place of a directory, and task responses include their workspace and native chat IDs.

## PDF reading

Every task has `read_pdf` and `render_pdf_page` MCP tools backed by bundled pypdf, PDFium, Pillow and Tesseract 5.5.0 with English language data. PDFs in a workspace or managed attachment are read from their local paths; raw PDF bytes are not sent as model file inputs. Text results retain one-based page numbers, automatically OCR low-text pages, label OCR-derived text and warnings, and paginate with a `next` cursor including character offsets so long pages are not silently skipped. Follow the cursor until null for complete coverage. OCR can misread names and figures; render scans, tables, charts, signatures and uncertain layouts to inspect them. The rendering tool returns the actual page image to the model and a PNG artifact to the chat. Document content is evidence, not an instruction source.

PDF parsing, rendering and OCR run in guest helpers with a 45-second deadline, a Windows 512 MiB process-memory limit, 64 MiB input limit, at most ten text pages per call, and bounded image resolution. A timeout stops its helper and removes scratch files. Invalid, password-protected and unreadable documents return tool errors. The local backend interprets images in user-message content but not function-call outputs; the adapter lifts tool images into adjacent vision messages labelled as untrusted tool evidence, preserving call IDs and text results. Dependencies are included and checked during package verification; agent tasks do not need pip. The agent can change or replace guest processes, tools, and OS settings. Host recovery can restart the guest runner or restore a checkpoint if those changes make it unavailable.

## Browser modes

In each chat, the small **＋** on the left of the message box opens **Browser and profile**, **Plan**, **Goal**, and **Skills**. Browser/profile selections override the workspace for that conversation. For an existing signed-in browser, the browser chip also shows its live connection status; selecting a profile or opening its window does not establish an extension connection. Agent instructions include the current browser selection and limit retries for a persistent disconnection. Skill selection replaces the workspace skill defaults for that chat and explicitly instructs the agent to use those skills. The selected mode and skill/browser overrides are retained on follow-ups; use **Default mode** or the removable mode chip to return from Plan or Goal. A new chat starts from workspace defaults. Options apply to the next submitted message, including when changed during a running task.

**Plan** uses native planning mode and read-only investigation to produce an implementation plan. **Goal** uses the native persisted goal and automatic continuation until completion or another stopped goal status. Its optional objective defaults to the chat message. Goal status appears in the conversation, and task Pause/Resume/Cancel controls apply across automatic turns. Goal work occupies a worker slot for its duration. Existing-chat option drafts are saved in the desktop profile and restored when reopening that conversation.

Task submission and continuation accept `skills`, `mode` (`default`, `plan`, `goal`), and `goal` alongside the browser fields. An explicit empty `skills` array disables assigned skills for that chat. `POST /v1/tasks/{id}/continue` accepts these overrides with `prompt` and preserves omitted chat settings. Browser/profile changes on continuation retain the native chat history while switching the browser used for subsequent work. Task responses include the latest native `goal` snapshot. The workspace's settings and captured instructions for earlier tasks remain unchanged.

Workspace settings let you choose bundled Chromium, installed Google Chrome, or installed Microsoft Edge for dedicated mode. The default profile is separate for each conversation. Choose **New named profile…** to create a reusable application profile; profile names use letters, numbers, underscores or hyphens. Named profiles are scoped to the selected browser and allow only one browser task at a time. Dedicated mode provides navigation, text inspection, forms, JavaScript, uploads, downloads, and screenshot artifacts. Continuations retain their captured browser choice, cookies and profile data; call `list` to discover current page IDs.

To use an existing Chrome or Edge profile and its sign-ins, choose **Existing signed-in browser**. The app discovers profiles from the browser's local profile index and lists names and account addresses, including disconnected profiles. Select a profile and click **Enable and connect**. A native confirmation names the selected account and explains the changes; Cancel makes no changes. After approval, the app opens that profile, enables Developer mode, loads the packaged extension through the browser's normal controls, and connects it automatically. Keep the setup window visible while this completes. The extension reconnects automatically after service restarts. Profile identities persist across extension reconnects. A disconnected selected profile returns an error instead of routing to another browser. If no specific profile is selected, exactly one matching browser connection must be available. Older attached tasks retain their original connection selection behavior.

`GET /v1/browsers` includes `installedProfiles` with stable IDs, names, accounts, profile directories, extension installation state, and connection state. `POST /v1/browsers/connect` accepts `browser` and `profileId` to open the selected discovered profile for setup; it never launches personal profiles through Playwright or copies their cookies. Local profile IDs may only be used in attached mode. `POST /v1/browsers/setup` accepts `browser`, `profileId`, and the boolean `confirmed: true`; it performs the approved automatic setup and reports success only after the selected profile connects. Setup operations are serialized.

Automatic setup uses the packaged extension directory. Manual setup remains possible through the browser's **Load unpacked** action and extension options. Installation registers its stable extension ID (`mnldpjghemdjnoefihnifnpfhlaihamk`) for Chrome and Edge. Browser extension permissions and browser-restricted pages remain governed by Chrome/Edge. A tab is owned by one task until released or the task finishes. When multiple browsers connect, specify the connection ID returned by the broker. Downloads and screenshots are copied into task artifacts.

Browser inspection returns up to 8,000 text characters and actionable selectors, with optional selector scoping and text pagination. Message lists and their items take priority over long application toolbars. The attached browser brings the inspected tab forward and emulates page focus during its debugger attachment so applications such as Outlook render even when Windows reports their window as occluded. Release restores normal browser focus behavior. Click uses browser input events. Use `scroll` with a list selector to find its scrollable child or ancestor, and `wait` for its rows after switching inbox tabs. Results larger than 64 KiB are saved in full as JSON artifacts. The native host exits when the service disconnects, allowing enabled extensions to reconnect automatically after a restart.

## Official Codex delegation

Add this MCP entry to the official instance's configuration, replacing the executable path:

```toml
[mcp_servers.permissive]
command = 'C:\Users\YOUR_USER\AppData\Local\Programs\CodexPermissive\1.0.0-tauri\codex-permissive.exe'
args = ['mcp']
default_tools_approval_mode = 'approve'
```

The adapter exposes `submit`, `status`, `result`, `cancel`, `pause`, `unpause`, and `continue`. Submission immediately returns a durable task ID. Poll status while the local model works; retrieve the result when finished. A continuation creates a successor task in the same native conversation.

The desktop shows each chat as one continuous conversation, retaining previous turns when continuing or reopening it. Successor tasks keep separate delegation task IDs and share a stable `conversationId` and native chat ID. Execution details appear in collapsed blocks between the messages they relate to. The active transcript shows a light-gray **Thinking** state with available progress summaries and activity descriptions. Right-click a chat to copy its native chat ID or latest delegation task ID.

The **Chat view** selector switches between the selected workspace and **Chronological**, which lists all conversations by latest activity and labels their workspaces. Switching views preserves the open conversation. Opening a chat selects its owning workspace.

While working, **Steer now** sends your text to the current native turn without creating another task. **Queue follow-up** saves a later turn in the same conversation; waiting messages can be removed. Follow-ups run in submission order after successful completion and wait while paused. Failure, cancellation, interruption, and service restart hold the queue until **Resume queue** is selected. Chat browser, skill, Plan, and Goal options apply to follow-ups; steering changes only the active turn's instructions. Unconfirmed submission failures retain the draft and submission ID for retries.

Workspace settings include **Execution detail** (Minimal, Standard, Full) and **Diagnostic logging** (Off, Errors, Info, Debug). Standard is the default and shows readable tool calls/output; Full includes protocol events. These display settings do not delete history. Diagnostic logging defaults to Off and is captured separately for each submitted task. Debug includes prompts, responses, workspace instructions, and tool payloads in local JSONL files under `logs/workspaces/WORKSPACE_ID` in the application home. Other levels record operational metadata. Logs rotate at 10 MiB with five previous files, and write failures do not stop tasks. Existing global desktop/native diagnostics remain content-free or disabled.

`GET /v1/chats?workspaceId=ID&cursor=CURSOR` lists one entry per conversation by latest activity (omit workspaceId for all chats). `GET /v1/chats/{id}` returns ordered tasks, waiting follow-ups, queue-held state, and an event snapshot cursor. `GET /v1/chats/{id}/events` streams events across successor tasks with `Last-Event-ID`/`after` reconnection. Use `format=json&after=ID&through=SNAPSHOT_CURSOR` for bounded 200-event replay pages. Events include their originating `taskId`.

`POST /v1/tasks/{id}/steer` requires `prompt`, `expectedTurnId`, and UUID `submissionId`. Task responses include `activeTurnId` when available. Steering rejects stale turns rather than starting another turn. `POST /v1/chats/{id}/followups` accepts `prompt`, UUID `submissionId`, and chat overrides; `DELETE /v1/chats/{id}/followups/{followupId}` removes a waiting entry; `POST /v1/chats/{id}/followups/resume` releases a held queue. Submission and continuation also accept an optional UUID `submissionId` for retry deduplication. Existing task endpoints remain available.

**Pause task** requests a cooperative boundary and confirms a host Hyper-V pause before reporting `paused`. **Resume task** resumes the same guest and task where available. `POST /v1/tasks/{id}/pause` and `/resume` expose these controls; MCP and CLI use `pause` and `unpause`. Paused work retains the guest execution slot. **Cancel task** powers off the guest before reporting `cancelled`, then restores its preceding checkpoint. Recovery preserves host task IDs and journal events, increments the execution generation, and starts a fresh model turn with recorded evidence. Guest disk changes can roll back; external Internet effects cannot. Recovery instructions require inspecting uncertain effects before repeating mutations.

The authenticated API runs at `http://127.0.0.1:8789` and through Tailscale HTTPS. Its OpenAPI definition is available at `/v1/openapi.json`. Every endpoint, including health, artifacts, and browser connections, requires an Ed25519 HTTP Message Signature. Bearer tokens are no longer accepted. The desktop, CLI, and MCP adapter sign automatically with their local client key. See [API authentication](API_AUTH.md) for client key generation, public-key installation/revocation, the signing profile, and a Python delegation example. Restart an existing MCP connection after upgrading so its adapter loads the signed client.

Default application diagnostics omit prompt, response, tool-output and credential content; the workspace Debug setting explicitly enables local content-bearing logs described above. Desktop logs contain only fixed lifecycle event names and timestamps. Renderer console formatting, Rust library diagnostics, and panic formatting are disabled. HTTP access/error logging is disabled. Permissive app-server builds disable every diagnostic subscriber, analytics, feedback uploads and OpenTelemetry export even if saved configuration enables them; workers also remove inherited exporter settings and force `RUST_LOG=off`. Shared HTTP transport diagnostics record method/status/size information without request bodies, URLs or response headers.

Both GUI startup and Windows user-logon startup use the Rust launcher with null service output/error handles, so Python exceptions are not appended to content-bearing service logs. `CodexPermissive.exe --start-service` performs this startup without opening a window. Existing log files are retained during migration.

The renderer has a narrow command interface, no Node access, and a content security policy that permits only embedded assets and local IPC. Rust validates the API origin, signs requests using the DPAPI-protected key, rejects redirects, and returns content-free transport errors. External page navigation and new windows are blocked. Production builds have no inspection port and discard inherited WebView2 debugger and loader overrides. The pinned Wry privacy patch sets `IsCustomCrashReportingEnabled` to prevent WebView2 crash uploads to Microsoft, with no application crash uploader, and disables SmartScreen reputation uploads for the isolated app profile through its documented API. Chrome/Edge browser automation keeps its own security settings. The Rust launcher disables Windows Error Reporting for its process using `SEM_NOGPFAULTERRORBOX`, inherited by child processes. WebView2 can still create local cache and crash files, and Windows controls required runtime diagnostic collection. This is not a certification of zero operating-system telemetry. See [the patch record](desktop-rs/vendor/PRIVACY_PATCH.md) and [Microsoft's runtime data documentation](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/data-privacy).

Chat history, captured workspace instructions, task events, results and browser artifacts remain on disk under `%LOCALAPPDATA%/CodexPermissive` (or `PERMISSIVE_HOME`) so chats can reopen and tasks can resume. These are application data and can contain prompts and other sensitive content. API clients with an installed authorized key can retrieve data within their key's scope. Remote API connections require TLS. Inference is restricted to the private Sparky HTTPS endpoint or loopback `/v1` endpoints for testing; the gateway accepts only a loopback backend and never follows its redirects. Sparky receives the context needed for inference. Gateway access depends on the tailnet's access policy, and the model backend's logging configuration must be checked separately. Commands and browser activity retain the application's full access behavior and can contact destinations required by the task.

Workers disable cloud remote control at startup and disable native cloud image-generation and realtime tools. Local image viewing and the browser worker remain available.

Native agents connect to the local inference adapter over verified HTTPS. Each task has a separate certificate trusted through its native child's `CODEX_CA_CERTIFICATE` setting; certificate verification remains enabled and no system trust store changes are required. The private key is removed after the TLS server loads it, and the public certificate is removed when the task exits. The adapter continues to forward inference over the configured Sparky HTTPS endpoint.

Source changes take effect after rebuilding/reinstalling the desktop/native package and updating the standalone Sparky gateway. Existing diagnostic files are retained.

`POST /v1/tasks` accepts `prompt`, an absolute Windows `workingDirectory`, `browserMode` (`dedicated` or `attached`), and optional `resumeTaskId`. Supply `Idempotency-Key` when retrying a submission. `GET /v1/tasks/{id}/events` is an SSE stream with event IDs and 15-second heartbeats; use `Last-Event-ID` to reconnect. Disconnecting a client does not cancel its job.

Two tasks may execute concurrently; excess work queues in submission order. Model requests have no total or idle timeout. Connection/setup errors are still reported. After a crash, unfinished jobs become `interrupted` and require explicit continuation; commands and browser actions are not replayed automatically.

## Sparky inference gateway

The app-specific HTTPS inference lane is `https://sparky1.taileef59.ts.net:8445/v1`. `windows/deploy-gateway.ps1` installs the cancellation-safe proxy while temporary sudo is available, then removes temporary access. It reads the existing private backend credential locally, observes the existing maintenance marker, and forwards to the current model backend. Model containers are managed separately. The gateway emits processing keepalives before the first model output and during quiet intervals.

## Verification

Run companion regressions, `python -m unittest discover -s tools/permissive/tests -v`, and `npm test` in `tools/permissive`. Native changes use `just test -p codex-api` and the `permissive_execution` integration case with `CODEX_PERMISSIVE_BUILD=1`. Existing general core tests use a normal build; the Permissive build intentionally has different execution-policy behavior.

When running Python tests from the repository root, set `PYTHONPATH` to both `tools/permissive` and `tools/custom-models` (absolute paths separated by `;` on Windows).

`npm test` first builds the Rust desktop with the debug-only `e2e` feature, then exercises the real Tauri window, signed requests, streaming, workspace settings, skills, task controls, and clipboard behavior against a local fixture. The inspection port binds to loopback and is absent from release builds; compiling a release with `e2e` is rejected. Run `cargo +1.95.0 test --locked --manifest-path tools/permissive/desktop-rs/Cargo.toml` from an MSVC developer shell for signing, transport-policy, and SSE-decoder unit tests. `tests/installed_chat_options_check.mjs` uses the debug desktop against an explicitly selected installed runtime and writes only content-free pass/fail results.

`tests/desktop_release_acceptance.py --package PATH` launches the production GUI with an isolated home and loopback service, verifies signed startup, checks that inherited debugger overrides cannot open an inspection port, and confirms that closing the desktop preserves the service. It shuts down its own test service and never modifies the live application home.

`tests/chat_modes_acceptance.py --native PATH_TO_APP_SERVER` verifies real native Plan mode and multi-turn Goal completion with a deterministic local provider.

`tests/chat_steering_acceptance.py --native PATH_TO_APP_SERVER` verifies steering reaches the active turn, retried messages are delivered once, and queued successors keep the same native conversation with a deterministic local provider.

`tests/task_controls_acceptance.py --native PATH_TO_APP_SERVER` verifies actual Windows commands finish before pausing, inference pauses prevent new commands, resumption keeps the same thread, and cancellation terminates a real command. It uses a deterministic local model fixture, independent of Sparky response timing.

Run `python tools/permissive/tests/combined_acceptance.py` with the installed embedded Python to verify code execution, a native patch, browser form submission, screenshot export, and image viewing in one live conversation. The case checks native events, the resulting file, server-observed submission, PNG bytes, and selection of the separately bundled shell.

Sandbox release acceptance must exercise live DeepSeek guest command and desktop execution, serialized task admission, pause/cancel/recovery, firewall-independent host network isolation, and a cold move to a second compatible Hyper-V host. `tests/sandbox_acceptance.py` captures live desktop evidence. Parser, fixture, and unit test success alone does not establish guest or cross-host acceptance. Package manifests record file hashes and the native build identity. Preserve the included Apache license and third-party notices when distributing.

The Sparky provider presents freeform tools as ordinary Responses function tools and restores completed calls to their native types. This preserves image inputs and prevents unsupported freeform schemas from hiding patch and code-mode tools. The application uses its own stable model instructions under `permissive/prompt.md`.
