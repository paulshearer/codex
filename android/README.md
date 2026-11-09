# Permissive Codex for Android

Version 0.1.12 follows the new reply after Regenerate and keeps the reading
position when retained activity loads or the branch is saved. Scrolling up to
read pauses automatic following. Generation no longer claims to be preparing
history; a brief **Saving reply…** status appears only if saving after generation
takes more than 1.5 seconds, and disappears when saving finishes.

Routine conversation restoration stays in Activity. The app removes
temporary recovery notices when work resumes, and handles expected connection
cancellation without displaying an error. Interrupted message edits and deletes
retry with the same submission ID, and successful changes close the dialog
before refreshing the chat list. Windows reuses the native conversation after
compaction instead of importing retained history for every reply.

Regenerate starts in one click and preserves each response as a
selectable version in the same chat. **‹ Reply 2 of 3 ›** restores that response
and its later conversation. It keeps earlier messages visible and streams output
while older activity loads in parallel. It opens only the latest numbered chat segment. **Load previous
segment** at the compaction boundary prepends one earlier segment and preserves
the reading position. Message actions retain the selected segment ID, including
after refreshing or reconnecting. Install over the existing app to preserve
pairing and drafts.

Message Delete and Delete from here open a new chat with the
requested changes and original title. Windows hides the original immediately and
securely erases it in the background, retrying after errors or restarts. The phone
can continue without waiting for the seven overwrite passes; unsent draft text,
attachments, and browser selections follow the replacement chat.

Native Android companion for an existing Permissive Codex Windows install, with a
Sparky Chat inspired Material 3 interface. Workspaces replace personas. The phone
shows the same Windows conversations, live answers, thinking, tool activity,
statistics, queued follow-ups and downloadable artifacts. Windows owns all model
execution and saved transcripts.

Active thinking streams inline as text arrives. Finished thinking and tool steps
show one sentence, with **View details** to expand the saved text. A small
**Attachments (N)** button at the bottom right opens the chat's attached and
generated files for saving; the conversation does not list them inline.
The status badge shows **Running (~N TPS)** in blue, **Failed** in red and
**Done** in green. TPS is the rolling two-second output estimate used by
Statistics and returns to zero while waiting for more output.

## Pair and use

1. Open **Pair Android → Create pairing QR** on the Windows install to create a
   short-lived invitation. Keep Windows and its Tailscale connection running.
2. Connect the Tailscale Android app to the same reachable tailnet as Windows and
   grant its VPN permission.
3. In Permissive Codex on Android, select **Pair**, scan the invitation locally or
   paste its JSON. QR decoding runs entirely on the phone through bundled ZXing.
4. Select a workspace tile and an existing chat, or open **New chat** in the drawer.
   Saved Windows installs can be selected from **Windows installs**.

The composer offers Continue for completed chats, Queue follow-up for active chats,
and explicit Steer active turn. Pause, Resume, Cancel, Remove queued follow-up and
Resume queue act on the Windows conversation. Cancel requires a confirmation.
Use **+ → Browser and profile** to choose the browser and profile on the paired
Windows execution host. The current choice is also shown above the composer.
Choices apply to the next new message, continuation, or queued follow-up; steering
and retrying a saved delivery retain the active or originally submitted settings.
Browser choices are encrypted locally for each host and chat.

Recognized premature response streams recover automatically with up to two
continuation attempts on Windows. Active retry progress appears briefly in chat;
original details remain in Activity. If recovery is exhausted, **Retry response**
continues the latest failed task from its saved conversation while preserving
your unsent composer draft. It does not resubmit the original prompt or files.

Use a drawer chat's actions menu or **More actions → Rename chat** to change its
name. Names are shared with Windows and persist through continuations and restarts.
Use a single line of 1–120 characters. **Copy chat ID** in the same menus copies
the native Codex thread ID, matching Windows. A loaded ID can be copied offline;
the action becomes available once the task starts.

Delete a chat using its drawer actions menu or **More actions → Delete chat**;
**More actions → Delete workspace** includes all its chats. Confirm with
**Delete permanently**; no typed confirmation is required. Windows must have no
active tasks during secure deletion. A successful deletion also clears this
phone's affected drafts and browser choices. The same seven overwrite passes as
the Windows app apply to app-owned saved data; separately saved files, SSD
remnants, snapshots and backups are outside that operation.
Connection recovery resumes event delivery from the last event ID and preserves
drafts. Failed submissions are never replayed automatically. A manual retry of the
same saved submission reuses its idempotency ID and original task/turn target,
including after an app restart. Refresh Windows state first if delivery is
uncertain, then explicitly select **Retry saved delivery** or **Clear draft**.

Attach up to 10 original files or photos, 16 MiB each and 32 MiB combined. Files use
Android's document picker and photos use the installed camera app with a private
temporary destination. No OCR, resizing, external model request, broad storage
permission or public-web fallback is involved. Camera access is requested only
when starting local QR scanning or taking a picture. Captures are removed after
import/cancel and abandoned captures are removed at startup. Original pending
attachment bytes stay only in memory. Already uploaded attachment IDs are saved in
the encrypted draft. After a process restart, any originals whose upload did not
finish are marked for reselection and sending is blocked until they are replaced
or removed.

## Temporary website credentials

Use **+ → Secure credentials** to paste or type a username and password manually.
Enter a label and an HTTPS website origin, choose **Session** or **1–168 hours**,
then **Provide securely**. The password remains masked. Entering or pasting values
does not submit them; only **Provide securely** sends the encrypted credentials to
the selected workspace. The same screen lists credentials and supports revocation.

These inputs are memory-only, excluded from saved drafts and Android saved view
state, and protected against screenshots/recent-app previews while the dialog is
open. Inputs clear on submission or close. Pasting uses Android's standard manual
paste action; the app does not proactively read the clipboard. Each upload uses a
fresh AES-256-GCM key, wrapped to a single-use RSA-OAEP-SHA-256 guest key with
explicit MGF1-SHA-256. The controller forwards ciphertext; the execution service
keeps each accepted entry encrypted under a separate memory-only key. Codex uses
the browser's secure fill operation on that registered HTTPS website, including
related sign-in subdomains at the same port. Separate websites and hosted tenants
remain isolated; internal names and IP addresses retain exact-origin matching.

Entries are available to tasks in the selected workspace. Revoke one or all at
any time. Session lasts until revocation or execution-service restart; restarting
also clears timed entries early. Finishing a turn or closing the phone app does
not end the execution-service session. Expiry stops new credential access, and
does not sign websites out. See the [security model and API](../tools/permissive/README.md#temporary-website-credentials)
for memory-handling limits and deployment requirements.

## Trust and storage

All API traffic is HTTPS to the paired `.ts.net` origin and must resolve only to
Tailscale addresses. Redirects, proxies and public endpoints are rejected. The
Windows gateway verifies the phone's enrolled per-installation Android Keystore
signature. Tailscale restricts network reachability; no email or Tailscale account
identity is used as application authentication. The app contains no model credentials or Tailscale auth
key. Forgetting a host removes its local pairing and drafts; revoke it on Windows
to block that device's future access.

Only host pairings, selection and composer drafts (including pinned pending
submission and staged upload metadata) are stored locally, encrypted
with Android Keystore protected AES-GCM in the no-backup directory. Transcripts,
event replay and pending attachments are memory-only. Android backup and device
transfer are disabled. Saving an artifact uses Android Save As and creates an
ordinary file at the user-selected destination.

## Build

Requires JDK 17, Android SDK 36 and the included Gradle 9.4.1 wrapper. Existing local
tools are located by `tools/android-env.ps1` under
`%USERPROFILE%\.androidexpenses-tools`. No Android Studio installation is required.

```powershell
cd C:\git\codex_permissive\android
. .\tools\android-env.ps1
.\gradlew.bat testDebugUnitTest lintDebug assembleDebug --no-daemon --max-workers=2
```

Debug installs side by side as `com.pshea.permissivecodex.debug`. Production uses
`com.pshea.permissivecodex`, version 0.1.10, supports API 26+, and targets API 36.

Create this app's independent signing identity once, then build a verified APK:

```powershell
.\tools\new-release-keystore.ps1
.\tools\build-release.ps1
```

The release script runs unit tests and debug/release lint, assembles the release,
verifies its signature and reports SHA-256. Output:
`out\PermissiveCodex-v0.1.10-release.apk`. The release keystore and its current-user
DPAPI password are stored outside the repository in
`%USERPROFILE%\.permissive-codex-android-secrets`. Back both up securely; future
updates require the same signing identity. Signing secrets are never committed.

For device-level tests, use an explicitly selected local emulator:

```powershell
.\tools\test-emulator.ps1 -Serial emulator-5580
```

This script rejects physical-device serials. It covers real Keystore signing,
encrypted storage, workspace selection, disconnected drafts, explicit steering and
selectable Markdown table rendering. Credential tests exercise the actual Android
Autofill service/authentication/Dataset path into the native activity hierarchy,
then review the lifetime without sending to a live host. The debug-only test vault
service is temporarily selected and the previous Autofill provider is restored
after the test; these fixtures are excluded from the release. Activity recreation
must discard both credential fields, and the production activity must set
`FLAG_SECURE`. No phone install is performed by a release build. See [NOTICE](NOTICE)
for source attribution.
