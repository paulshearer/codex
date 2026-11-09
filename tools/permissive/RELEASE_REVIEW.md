# Release review: Codex Permissive 1.0.0

## Repository publication checks — November 8, 2026

The complete standalone application, reusable provider adapter, Windows delivery
scripts and Android companion source are included on `standalone-v0.162.1`.
The root README distinguishes new VM deployments from explicit connections to
existing host installations and links the sanitized harness regression report.
Local attachment staging, runtime output, credentials and backups remain ignored.

Before publication, the full application Python suite passed 450 tests with one
optional skip using the service virtual environment; all 86 provider tests
passed. The browser/extension/privacy checks passed 27 tests with one optional
browser skip, and the UI production asset build passed. All 26 native source
hashes matched the deployed package's build manifest. Staged whitespace checks
passed. The legacy telemetry-migration test now constructs the original central
event-storage layout and closes its databases even when an assertion fails.

## Asymmetric attention restored — November 8, 2026

Restored the retained native `grouped_24_24_16` attention deployment on all three
Sparks, with DSpark block 5, eight running requests and graph size 8. Runtime
inspection confirmed 24/24/16 attention heads, the native sparse-attention kernel,
and loaded target/draft models with verified weight slices. The installed harness
has exactly the same thirteen file hashes as the padded-version evaluation.

The same six device/browser scenarios and two installed signed-API checks passed:
8/8, with zero malformed tool calls, zero no-op shell commands and zero recovery
continuations. All 83 focused tests and 11 API checks also passed. The final
installed device and browser-blocker checks took 15.52 and 8.33 seconds. The
existing paused chat stayed paused. Test traffic used local mock devices; no real
appliance settings or firmware were changed.

The padded containers were retained and independently committed, with saved boot
files and SHA-256 state/cache archives on every node under
`/var/lib/tp3-paddingfree-20261030/saved-padded-asymmetric-rerun-20261109-002250`.
The existing sealed runtime archives also remain available. All model-routing
checks passed. The old October Tailscale reference differs only by the existing
HTTPS 8446 APK download route; that route and the historical reference were
preserved, and live routes hashed identically across testing. The raw audit and
the qualified acceptance result are both retained.

Evidence and matched timings: `out/asymmetric-rerun-20261109-002250/comparison.md`.
The padded baseline uses requests 1/graph 4, so these are saved-deployment quality
checks, not a controlled attention-only speed or numerical-equivalence study.

## Existing-host harness update — November 8, 2026

Updated the installed host backend at `1.0.0-tauri-20261013-pdf` with code mode
only, an unambiguous provider JSON envelope, bounded contract correction,
browser/desktop capability context, typed HTTP requests, a shorter neutral
Windows prompt, and generation-stall recovery after output has begun. Initial
prefill remains unbounded. The missing desktop worker and compiled native agent
are now installed. Package verification checks these required helper files.

Validation passed 83 focused tests, the concurrent real native-app-server
acceptance check, and 12 real DeepSeek runs using automatic tool selection:
four initial device/browser scenarios, six extended scenarios including existing
login and a connected browser fixture, and two final checks through the installed
signed task API in the existing workspace. Full event audits found no malformed
calls, no no-op shell loops, and no recovery continuations in the fixture runs.
Setup occurred once; subsequent login reused the configured device. HTTP timeout
fallback, token-reference authentication and truthful browser blockers passed.
The installed checks took 17.64 and 10.46 seconds. All device traffic targeted
disposable loopback fixtures; no real device firmware/settings were changed.

The live update preserved the installed host-specific service implementation,
its process-sweep guard, existing desktop/mobile clients, and the padded Sparky
model deployment. Only the service capability hook was added to that entrypoint.
The original 889 task metadata rows and 16 workspace metadata rows had identical
hashes across restart; the saved paused task stayed paused. Two labeled test
chats were then added to the original workspace. The original selected Edge
extension remains disconnected and requires the user's connection action.

Backup, per-file before/after hashes, full fixture evidence and verification logs:
`out/harness-upgrade-20261108-234509`. The existing package passed full manifest
integrity before the update; the updated package passed integrity and startup
verification afterward. Canonical model results were saved and rendered under
`out/model-test-results`.

## Existing-host client update — October 18, 2026

The Windows desktop was updated at
`%LOCALAPPDATA%/Programs/CodexPermissive/1.0.0-mobile` using an explicit
existing-service connection to the original signed API on loopback 8789. The
desktop and pinned shortcuts point to the updated client. The original backend
process and its user-logon startup entry were preserved. The updated client
authenticated successfully without initializing or starting an execution
backend. All eight existing history tables have identical hashes: 87 tasks,
14 workspaces and 357,767 events remain intact. Existing native conversations
and host project folders continue through that same backend.

This client update requires no VM provisioning or history migration. The
earlier provisioning attempt was stopped before a VM was created, and its
unused disk and credential envelope were removed. Default bound-controller
deployments retain their strict role and VM checks. The older signed service
is accepted only by the explicit local client connection and mobile gateway
opt-in for `http://127.0.0.1:8789`.

The signed Android 0.1.0 release was installed on the confirmed Samsung
SM-S948U (Android 17), and its installed APK hash matches the release below.
Cold launch and force-stop/relaunch passed. Tailscale VPN was already active.
Google Reading Mode and its two-finger shortcut mode were restored and verified.
Client compatibility passed ten Rust tests; the mobile legacy integration
suite and Windows helper tests cover signed identity/forwarding and strict
role rejection. The user completed local QR enrollment; Windows confirms one
enrolled phone and a running tailnet-only gateway. Unauthenticated HTTPS access
returns 401. The scheduled helper uses the same physically resolved data store
as the desktop's Store-packaged launch context, preserving enrollment across
native and packaged launch contexts. Six Windows helper tests cover this data
binding and native stderr handling. Final installed-package integrity and
startup verification passed.

## Android companion 0.1.0 — October 18, 2026

The native Android companion uses Sparky Chat's workspace-tile styling and shares
the Windows controller's chats, task events, queue controls, attachments and
artifacts. Windows and Android keep independent selections. Android stores
encrypted host preferences and drafts; transcripts and pending attachment bytes
remain in memory. Pending submissions retain their original target and
idempotency ID across restarts, and retries require an explicit user action.

Local Windows **Pair Android** creates a five-minute, single-use invitation. Each
phone enrolls a separate Android Keystore P-256 key for each Windows installation.
Signed requests cover their origin, path/query, body digest and installation ID,
with durable nonce replay protection. The companion gateway listens on loopback
8790 through tailnet-only Tailscale Serve HTTPS 9444. Tailscale provides private
connectivity; the enrolled device key provides authentication, with no email
identity check. Revocation, disabling and pairing management are local Windows
operations. The existing Ed25519 API and its port 9443 remain separate.

Signed release: `android/out/PermissiveCodex-v0.1.0-release.apk`, package
`com.pshea.permissivecodex`, Android API 26+, target 36. SHA-256:
`2267EB5662ED759F51A7AB688E3D7E4032B25F30025BB2C91B98F90F03E3C677`.
The matching Windows distribution is
`C:/codex-build/CodexPermissive-Mobile-0.1.0`.

Validation: the Python suite ran 286 tests with one optional skip; the final
29-test mobile suite, three Windows companion tests and portability regressions
also passed. Six Rust tests passed. The focused Node/browser suite had 19 passes
and one optional Chrome skip; the pairing dialog was also tested separately.
Android passed 26 JVM tests, seven emulator instrumentation tests and debug/release
lint with zero errors. A real emulator Keystore signature was verified by the
production Python verifier, including rejection of its replay. Instrumentation
includes offline startup, immediate backgrounding and encrypted draft edits on
the actual ViewModel. Windows package integrity, native startup and standalone
CLI verification passed.

These initial build checks used an emulator. Subsequent physical-phone
installation and the existing-host Windows client update are recorded above.
See `android/README.md` for build, pairing and signing-key backup instructions.

## Hyper-V execution boundary — October 18, 2026

The controller now dispatches native model, desktop, shell, and browser work to
a dedicated Windows guest over PowerShell Direct. The host owns history,
generation fencing, checkpoints, inference transport, and vSwitch ACLs. A
controller deployment is required and the desktop/CLI verify the connected
role and VM identity before dispatch. Guest command-name denial rules are
removed. Artifact actions download data without opening host file associations.

The provisioning toolkit applies an official hash-pinned Windows 11 Pro image
to a Generation 2/version 9.0 VM with Secure Boot off and no vTPM or BitLocker
dependency. A portable cold bundle includes guest disks/checkpoints and a
consistent controller snapshot. Import regenerates host identity, credentials,
TLS and network bindings, and selected skills are copied and rebound. Export
fences the source before shutdown.

The host has explicit collection limits: 256 MiB/100,000 journal events per task,
8 MiB per event/status, 2 GiB per artifact, 8 GiB/256 retained artifacts per task,
64 MiB per transport response, and 1 GiB per host transport helper. These
constrain guest-to-host data; they do not classify guest commands.

Production desktop compilation, six Rust tests, and the full Python suite
(248 passed, one optional OCR skip) have passed. Package integrity/native/CLI
startup verification passed at `C:/codex-build/CodexPermissive-Sandbox-1.0.0`.
The official Windows 11 ISO was downloaded and matched Microsoft's published
SHA-256. Provisioning has not started: `PSHYPERV\pshea` is a standard account,
existing remoting cannot authenticate it for administration, and Windows UAC
is awaiting administrator authentication.
Live guest desktop/model, firewall-independent isolation, recovery, and a cold
move to a second Hyper-V host require separate elevated acceptance. The older
installation remains in place while its active task runs. Historical release
results below do not establish acceptance of this new VM boundary.

## Rust desktop migration — October 11, 2026

The desktop now runs in a Rust/Tauri shell with the existing React interface and Python task service. Electron's main/preload implementation and runtime dependency have been removed. Rust handles DPAPI-backed Ed25519 request signing, explicit desktop operations, task-event streaming, native dialogs, clipboard actions, artifact opening, and service startup. The renderer loads embedded assets with restricted IPC, blocks external navigation and new windows, and has no Node interface or production inspection port.

The new package is `C:/codex-build/CodexPermissive-Tauri-1.0.0`. It is installed at `%LOCALAPPDATA%/Programs/CodexPermissive/1.0.0-tauri`; the desktop shortcut and user-logon startup point there. The idle previous service was stopped, the new service started, and the Rust GUI reopened. All 29 saved task IDs were preserved. Hashes of every row in all four application database tables match the pre-upgrade SQLite backup, including workspaces, task events, and skill assignments. The backup is `%LOCALAPPDATA%/CodexPermissive/backups/rust-upgrade-20261011-155416`. Deployment evidence is `C:/codex-build/permissive-rust-upgrade-verification.json`. The previous installation remains available.

Both desktop and native production binaries were rebuilt with the current privacy protections. Diagnostics suppress prompt, response, tool-output, and credential content; service output/error handles are null. Native analytics, feedback, OpenTelemetry export, remote control, cloud image generation, and realtime tools are disabled. API redirects cannot forward signed prompt requests. A pinned Wry patch disables WebView2 crash and reputation uploads through documented APIs; the Rust launcher also opts out of Windows Error Reporting. Local history, runtime cache, and older diagnostic files remain. Windows controls required WebView2 runtime diagnostics, and Sparky model-backend logging has not been independently verified.

Validation passed: 64 Python service tests, six Rust desktop unit tests, and eight Node/browser/UI cases; the optional dedicated Chrome case was skipped. The production GUI acceptance verified headless startup, signed service access, absence of raw service logs, rejection of inherited debugger overrides, and service survival after window closure. Native acceptance verified Plan and automatic Goal completion, pause/resume boundaries, workspace prompt/skill propagation, continuation context replacement, and cancellation of real commands and descendants. Package and installed-package hash/identity checks passed. Windows launch scripts use explicit process waits so GUI executable checks cannot return early; version output tolerates closed pipes.

The following sections preserve evidence from the earlier Electron releases.

## Chat options and skills deployment

The follow-up Edge profile discovery repair is also deployed. Existing Chrome/Edge profiles now appear with account addresses even before an extension connects. Selecting a local profile exposes an exact-profile setup action, a copyable extension folder, and explicit pairing to the native connection. Extension connections automatically reconnect after service restarts, and disconnected local selections never fall back to another profile. Validation passed: 47 Python tests, the desktop IPC/profile-setup regression, and a real Chromium extension pairing regression. The installed Electron check verified all four existing Edge accounts and no page errors; both package integrity checks passed. Updated evidence is in `C:/codex-build/permissive-installed-chat-options-verification.json` and `C:/codex-build/permissive-installed-edge-profiles.png`. Deployment backup: `C:/codex-build/permissive-chat-options-backup-20261010-121843`.

The October 10 chat-options and skills update is installed in both the distribution and the live user installation. The desktop and inference service were restarted. The chat composer exposes browser/profile, native Plan mode, native Goal mode, and explicit skill selection through its left-hand + menu. The Skills library provides category grouping and ZIP import/export.

Both package manifests were refreshed, and full package integrity/native startup/CLI checks passed. All 25 existing tasks retained their IDs, native chat IDs, workspace assignments, statuses, and results. The consistent database backup and previous application files are at `C:/codex-build/permissive-chat-options-backup-20261010-120349`.

Installed Electron verification exercised all four menu options, browser/profile controls, 41 available skills, and the import/export library controls, with no page errors. The normal desktop launcher was then reopened and left running. Evidence: `C:/codex-build/permissive-installed-chat-options-verification.json` and `C:/codex-build/permissive-installed-chat-plus-menu.png`. Production native binaries remain unchanged and match the binaries used for the native Plan/multi-turn Goal acceptance tests.

Upstream baseline: `rust-v0.162.1`, commit `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`. Work is on `standalone-v0.162.1`; the earlier custom-models branch remains available. No changes have been pushed.

## Review boundaries

This application spans several separately reviewable components. The smallest coherent first stage is the pinned upstream baseline, existing companion import, native disabled-idle deadline, and compile-time execution-policy bypass. The companion import is mechanical apart from nullable inference deadlines, configurable RPC deadlines, and direct app-server launch support.

Review subsequent stages separately: durable task storage and authenticated scheduling; native worker lifecycle and MCP delegation; dedicated browser worker; attached-browser broker/extension; desktop UI; and reproducible packaging/install scripts. These boundaries follow the existing modules and avoid a single large implementation review. No native protocol or Rust dependency was added.

## Verified behavior

- Native release API suite: 213 passed. Focused API/provider checks: 246 passed. The native full-access integration executed a command forbidden by a local execution policy.
- Companion regression suite: 76 passed. Service/MCP/gateway/browser ownership tests: 10 passed.
- Playwright exercised dedicated Chromium forms, uploads, downloads, screenshots, and cookie persistence, plus an actual Chromium extension controlling an existing tab. Electron desktop IPC and isolation were exercised separately.
- Live Sparky inference completed concurrent code/browser tasks, wrote a Windows file, saved screenshot artifacts, continued a native conversation, and terminated a real child process on explicit cancellation.
- Official Codex submitted, polled, and retrieved a DeepSeek task through MCP. Its delegation permission is scoped to the Permissive MCP server.
- The Windows API required bearer authentication; Sparky accessed it successfully through the tailnet HTTPS endpoint.

The complete normal-build Rust workspace suite was run on October 10, 2026. Its initial result and focused followups are recorded below; it is not a clean workspace pass.

## Installation notes

The independent package includes native helpers, embedded Python, Node, Electron, and Chromium. Desktop launcher: `CodexPermissive.exe`; CLI: `codex-permissive.exe`. File names are distinct on Windows. Package verification checks hashes, native startup, and CLI identity, including after relocation.

Attached Chrome/Edge setup uses **Enable and connect** followed by one native confirmation. The app enables Developer mode, loads the packaged extension and connects the selected profile automatically. Dedicated Chromium is available immediately. The installer registers the native messaging host, desktop shortcut, user-logon service, and tailnet-only API proxy.

API clients control this Windows account's code and browser execution. The local model determines its responses; this application has no separate response-moderation layer. Unfinished jobs after a crash are marked interrupted and are never automatically replayed.

Final installed delegation completed as task `0f378a72-4db1-4cbb-a806-d357dc8dbefd`, wrote `OFFICIAL-INSTALLED-OK` exactly, and ran packaged Node `v24.19.0` and Python `3.13.7`. Installed desktop inspection confirmed that the input form remains inside the viewport with eight saved tasks. Scoped `just fix` and repository `just fmt` completed successfully.

A stricter combined acceptance exposed unsupported freeform tool schemas in Sparky's compatibility lane. The app now wraps them as function tools and restores native completed calls, with unchanged image contents. Companion tests: 76 passed, including wrapper integrity and rejection checks; real native function-wrapper integration passed. Combined installed DeepSeek task `886fc3d5-d5fe-4b2d-bcb4-3936725477fa` passed four commands, one native patch, browser list/open/inspect/type/click/screenshot, server-observed form submission, one image view, and PNG artifact retrieval. Commands used the independently bundled PowerShell 7.6.6. The report is saved under `C:/codex-build/permissive-acceptance/PERMISSIVE-COMBINED-153fab5db4f0/verification.json`.


Additional native verification: the normal-build core run selected 4,435 tests and passed 4,430 initially. All five failures were resolved and passed in focused reruns: the large-image fixture has a Windows local-run timeout budget of three minutes; the response recorder applies custom matchers before recording requests; Windows hook assertions allow cold-start time; and two v2 subagent fixtures call the v2 namespace. All 48 subagent cases passed, and all 52 test-support cases passed, including an HTTP regression that proves unmatched requests are excluded. These fixes affect test infrastructure and fixtures only, so the separately compiled production binaries remain unchanged. Logs: C:/codex-build/permissive-core-unprivileged-project-tests.log, permissive-core-last-diagnostic-tests.log, permissive-core-subagent-final-tests.log, and permissive-core-matcher-regression-tests.log.

That earlier core run skipped 89 cases: 77 upstream ignored cases and 12 cases held for Windows elevation. Five require symlink privilege; seven provision sandbox accounts and ACLs. The later full workspace run included elevated checks. The user withdrew the earlier AGENTS.md instructions on October 10, 2026. No unresolved failure remained among the earlier 4,435 selected cases after its focused reruns; the later full run exposed additional failures.

Fresh official delegation through the final installed tool wrapper completed as task 627b2d45-14e7-4ead-ae16-e2174f946fa3. It applied a native patch, ran a command successfully, and produced exactly 20 bytes (OFFICIAL-WRAPPED-OK followed by a newline). Official Codex retrieved the completed result. Independent verification: C:/codex-build/permissive-wrapped-delegation-verification.json.

Final scoped lint (just fix -p codex-core -p core_test_support --allow-no-vcs) passed in 12m 32s. Repository just fmt passed; unrelated upstream formatting churn was restored. Previously packaged production source hashes remain unchanged, and both package manifests now record the four added test-only source inputs.

Desktop broken-pipe repair: console diagnostics and stdio errors now write to a bounded local log, and cancelled event watchers settle normally without stale task updates. The Electron regression closes both output pipes, triggers a real IPC error, and confirms the window remains responsive. Updated desktop files were installed and package hashes refreshed; native binaries and the inference service are unchanged.

Full Rust workspace verification: `just test --workspace --locked --test-threads 4` ran elevated against the staged 0.162.1 baseline with `CODEX_PERMISSIVE_BUILD` unset. All test binaries compiled after installing the current-user GStreamer 1.28.6 development prerequisite. Nextest ran 20,569 tests: 20,490 passed, 78 failed, one timed out, and 139 upstream-ignored cases were skipped. The run took 45m 46s and reported 289 process/pipe cleanup warnings among passing tests. Original evidence is preserved in `C:/codex-build/permissive-workspace-tests.log` and `permissive-workspace-junit.xml`.

Focused verification resolved 50 initial failures so far. The remote-environment fixture now waits for its function-call event before steering; all 54 remote-environment cases passed after rebuilding. TUI followups used 32 MiB test-thread stacks for cases that overflowed the recipe's 8 MiB stack. Reviewed staged snapshot normalization changed only the release version and its wrapping/padding; root TUI fixtures remain unchanged. Git's existing Unix utility directory supplies `true` for the executor fixture. Restricting the test PowerShell module path to Windows PowerShell modules resolved the private-directory ACL case.

Twenty-nine initial failures remain unverified or failing: Windows sandbox setup/locking/MXC cases and a sandbox cloud-policy timeout; five Schannel-to-Rustls fallback cases; two non-version TUI fixture differences; a V8 prebuilt-library sandbox-feature mismatch; and one ACL mutation case awaiting Windows elevation. These counts describe focused reruns, not a repeated clean workspace run. The machine-readable report and exact remaining case names are in `C:/codex-build/permissive-workspace-verification.json`.

Only the remote-environment test fixture changed in the repository during this full-suite verification. Both package manifests record it as the fourteenth source input. Installed native helper hashes still match their package manifest; production binaries were not rebuilt for the test-only change.

## Task controls and conversation display

The October 10 task-control update is installed in both the distribution and the live user installation. Cancellation now owns the complete native worker process tree through a Windows job object, and request disconnection cannot abandon cleanup. Pause finishes the current activity and blocks the next inference/tool boundary. Resume continues the same live task and native conversation; paused tasks remain paused across service restart until explicitly resumed. An active paused task retains its worker slot and browser ownership.

The desktop provides Pause, Resume, and Cancel controls, plus a native right-click menu that copies the native chat ID or the separate task ID. Conversation messages aggregate assistant text without duplicating streamed completions. Raw command, reasoning, and protocol events are collapsed under expandable Execution details by default.

Validation passed: 77 companion tests, 15 service/API/MCP tests, and four Node/Playwright tests. Real native worker acceptance proved that the current command finishes before pause, the next command executes once after resume on the same thread, a pause during inference blocks the next tool, and cancellation terminates both a command and its grandchild. Native cancellation took 0.672 seconds. Evidence: `C:/codex-build/permissive-task-controls-native-verification.json`.

Installed live Sparky acceptance paused and resumed task `6b544594-a1ee-4a9a-8b45-68ada2518b0f` on chat `01a125ff-d616-7cb2-b619-2564df517507`, then cancelled an actual 120-second PowerShell command in 0.438 seconds with independently verified process exit. Installed Electron inspection verified collapsed details, visible assistant messages, expandable events, correct clipboard chat ID, and an input form inside the viewport. Package integrity, native startup, and CLI verification passed. The GUI was restarted and left running. Production Rust binaries were not rebuilt. Aggregate evidence: `C:/codex-build/permissive-task-controls-verification.json`.

## Workspaces

The October 10 workspace update follows EIF's flat Workspaces/Chats sidebar and settings layout. Each workspace owns chats and defines its directory, browser default, system prompt, and assigned local SKILL.md documents. The editor discovers personal/project skills and imports additional skill files through a native file picker. No swarm or hierarchy controls were added. API, CLI, and MCP delegation accept workspace IDs; legacy directory-only submissions automatically belong to a workspace for that directory.

Migration preserved all 18 existing tasks, including native chat IDs, results, events, timestamps, and statuses. Existing chats were grouped by directory, with a database backup at `C:/codex-build/permissive-workspaces-backup/tasks-before.sqlite`. Settings and explicit skill content are captured per task. Paused work retains that snapshot; subsequent tasks and continuations capture current settings. Native automatic skill instructions are disabled so only the assigned skill documents enter workspace instructions.

Native resumed histories preserve their initial developer bundle. The workspace facade therefore replaces its own marked context section and refreshes the captured current workspace instructions on every inference request, preserving user and tool history. This also clears superseded prompt/skill assignments on continuations while keeping the native chat ID. A native deterministic regression verifies the outgoing developer context, including exclusion of a valid unassigned project skill. The live Sparky continuation verifies the changed prompt actually governs the result.

Validation passed: 23 Python service/API/MCP/migration cases and four Node/Playwright cases (the desktop case passed in its focused rerun after accessibility-label fixes; the other three passed in the full run). Native acceptance covered prompt/skill propagation, replacement of stale continuation context, pause/resume, and process-tree cancellation. Installed Electron inspection verified workspace settings persistence and chat filtering. Live tasks `737a3e1f-d374-4aff-bcb5-a5a3222bc3e3` and `b8874873-70fd-4dd4-b396-9b275131381c` used workspace `fb19243b-0898-4283-9998-23b3f8b4d237`, continued chat `01a12616-9e27-7151-b77d-771ff4154c93`, executed code in the workspace directory, and followed the original and updated instructions. Package integrity/native startup/CLI verification passed after the final continuation repair. Both packages and the running installation are updated; production Rust binaries remain unchanged. Aggregate evidence: `C:/codex-build/permissive-workspaces-verification.json`.

## Automatic browser profile setup

The October 10 automatic extension setup is installed in the distribution and live user installation. Selecting an installed Chrome or Edge profile exposes **Enable and connect**. Its native confirmation names the account and explains Developer mode, extension loading and browser control; Cancel performs no setup. The packaged Windows helper uses the browser's normal extension controls and folder picker, then pairs the selected profile automatically. It does not rewrite personal profile settings or change browser-wide policies. The signed setup API requires the literal boolean confirmation, serializes native UI setups and waits for the exact selected profile's connection before reporting success.

Validation passed: 49 Python service/API cases, the focused Electron confirmation/Cancel/success regression, and native Edge acceptance starting with Developer mode disabled in an isolated profile. Native acceptance verified the extension persisted and connected through the installed native host. Both package integrity/native startup/CLI checks passed. Installed Electron inspection verified all four existing Edge accounts, the automatic setup button and confirmation, Plan/Goal, and the 41-skill library. All 25 saved tasks were preserved exactly. The GUI was reopened. Evidence: `C:/codex-build/permissive-browser-setup-native-verification.json`, `permissive-installed-chat-options-verification.json`, and `permissive-browser-setup-deployment-verification.json`. Backup: `C:/codex-build/permissive-chat-options-backup-20261010-125322`.

## Browser credential filesystem repair

The October 10 repair is installed in both packages. Store-packaged Codex and ordinary Edge native hosts now use the same DPAPI-encrypted client location under USERPROFILE, and derive the instance ID from the configured absolute Windows path rather than a filesystem-resolved AppData path. Local provisioning migrates existing encrypted identities and retains legacy files for running clients. Migration does not restore revoked authorization. Startup performs this migration even if the service is already listening.

The paused Outlook request had replaced the installed native-host manifest with a diagnostic wrapper and instrumented the extension background script. Both original components were restored, and the wrapper was backed up and removed. The running service and paused worker were retained. All 26 saved tasks remained exactly unchanged.

Validation: all 50 Python tests passed, including a regression covering different AppData views, redirected resolved instance paths, unchanged signing identities and revocation. A separate WMI-launched process outside Codex confirmed the browser signing key can be loaded and the restored executable is registered. The actual Argano Edge extension reconnected automatically as the exact profile selected by task a677f53f-9dc3-45f9-98d6-d969f56a4860. A signed browser command successfully listed its 25 tabs. Both package integrity/native startup/CLI checks passed after the final repair. Evidence: C:/codex-build/permissive-browser-auth-repair-verification.json. Backup: C:/codex-build/permissive-browser-auth-backup-20261010-135234.

## Outlook browser workflow repair

The October 10 repair is installed in both packages. Browser responses now retain useful page text and structured controls instead of cutting results at 900 bytes. Inspection returns up to 8,000 text characters, selector scoping and pagination, and prioritizes message lists over application ribbons. Oversized results are saved completely as JSON artifacts. Attached inspection brings the target tab forward so Outlook renders its virtual lists, and click uses trusted browser input events. Scrolling finds the actual overflowing ancestor and handles short lists without an error. Navigation supports new task-owned tabs, and wait supports asynchronously rendered controls.

The native messaging host now reads browser input through a daemon thread using raw pipe reads. It exits when the service closes its WebSocket even while Edge keeps stdin open, allowing the extension to reconnect automatically. Interrupted browser commands return a clear connection error. Agent instructions require checking the top of both Focused and Other, comparing received dates and reading the selected message body.

Validation: 53 Python tests passed, including actual native-host process termination and disconnect handling. Four browser tests passed, covering Unicode/full responses, artifact preservation, selectors, crowded ribbons, hidden tabs, scrolling, navigation and actual extension operation; the Chrome-specific managed-profile test was skipped because Chrome is not installed. Both package integrity/native startup/CLI checks passed. During a real service restart the old native host exited without being killed, and the selected Argano Edge profile automatically reconnected and returned Outlook inspection results.

The original native chat `01a12793-6440-7d73-b1ef-9d4ec947f041` completed its email request, and continuation `e178622d-90f4-4ec3-9e1a-adfa5d5ba213` repeated it after the final deployment and restart. It checked both inbox tabs, opened and read the full newest message, and corrected the earlier Focused reference. Independent inspection agrees: TradePub.com, "Paul, New White Paper: The State of Storage in the Ransomware Era," today at 9:18 AM is newer than Focused's Rapid7 investigation from Friday at 8:18 PM. All 27 pre-deployment tasks retained their IDs, native chat IDs, requests, workspace assignments, statuses, results and errors. The desktop was reopened. Evidence: `C:/codex-build/permissive-browser-workflow-verification.json`. Final deployment backup: `C:/codex-build/permissive-browser-workflow-backup-20261010-170955`.

The subsequent top-five email request exposed Windows occlusion: `Page.bringToFront` could leave the controlled Outlook document hidden even when Edge reported its window normal and its tab active. Enabling CDP page focus emulation restored `document.visibilityState` to visible and immediately loaded the live message list. The installed extension now applies focus emulation during inspection/control and disables it on release. Scroll resolves an overflowing descendant as well as an ancestor, covering Outlook's inner virtual-list scrollport. Browser regressions passed, including focused page state, restoration on release and an inner scrollport. Agent instructions explicitly require browser input operations rather than untrusted DOM clicks, and date checks for pinned messages.

Task `3ee148b5-3b68-4d41-94eb-41f8afeb2fb0` completed in the same native chat. It compared Focused and Other, opened all five newest messages and read their bodies: Scoty Griffin at 5:46 PM, Jason Andrews at 2:29 PM, Chandra Kasarabada at 2:28 PM, Rapid7 at 2:25 PM and Jason Andrews at 2:16 PM on October 10. No replies were sent or email attachments downloaded. The complete result and browser evidence are at `C:/codex-build/permissive-outlook-top-five-result.json`.
