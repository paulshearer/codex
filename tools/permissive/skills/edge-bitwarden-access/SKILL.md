---
name: edge-bitwarden-access
description: Use Edge with Paul's AI-specific Bitwarden CLI vault for website logins. Use when Codex needs to open the correct Microsoft Edge Work profile, retrieve site credentials from the Bitwarden CLI account created for AI use, fill login forms, handle OTP/MFA handoff including AA.com email verification, or avoid the known wrong Edge profile and Bitwarden-extension traps.
---

# Edge Bitwarden Access

## Core Rules

- Use the Bitwarden CLI, not the Bitwarden browser extension UI or Bitwarden web/app UI.
- Never print passwords, session tokens, full item JSON, OTPs after use, cookies, local storage, or browser profile secrets.
- Treat login form filling as user-authorized when the user asks to log into that specific site.
- When Paul explicitly authorizes creating an account or setting, changing, or submitting a credential for a named site and account, that authorization covers generating or retrieving the credential, saving or updating it in Bitwarden, entering it in the site's credential fields, confirming it, and submitting the account or credential form. Keep the credential in memory and never print it. This authorization is limited to the named site, account, and credential action.
- If the selected Bitwarden item can generate an OTP/TOTP, use it automatically for that site's MFA prompt without printing it. Stop for SMS, email, push approvals, unexpected permission prompts, purchases, or security-sensitive actions Paul did not explicitly authorize. For CAPTCHA, follow the task-authorization rule below.
- Paul's request to perform a task authorizes attempting CAPTCHA verification encountered within that task; do not ask for separate CAPTCHA approval. Attempt the visible challenge using the supported browser controls. If unsuccessful, blocked, or requiring a user-only action, notify Paul with the specific obstacle and request only the needed help. Do not repeatedly retry a failed challenge or bypass the verification.
- Follow higher-priority product and tool confirmation requirements. This skill records Paul's standing preference but cannot override a platform-mandated handoff or confirmation.
- Clean up temporary Bitwarden session files before finishing.

## Known Local Setup

- Bitwarden CLI path:
  `C:\Users\paul\AppData\Local\Microsoft\WinGet\Packages\Bitwarden.CLI_Microsoft.Winget.Source_8wekyb3d8bbwe\bw.exe`
- If missing, install with:
  `winget install --id Bitwarden.CLI --exact --accept-package-agreements --accept-source-agreements`
- Preferred temp session file:
  `$env:TEMP\bw-session.txt`
- Optional durable local master-password credential:
  Windows Credential Manager generic credential target `Codex.EdgeBitwarden.MasterPassword`, user `paul@informedcio.com`.
- Correct Edge profile for this workflow:
  `--profile-directory="Default"` in normal Edge, visible as `Work - Microsoft Edge`, signed in as `paul@informedcio.com`.
- Avoid the automation profile:
  `C:\git\monarchmoney\.edge-profile`, often on debug port `9223`, visible as `Profile 1 - Microsoft Edge`.

Do not launch personal logins in `C:\git\monarchmoney\.edge-profile`.

## Quick Workflow

1. Check helper status:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\paul\.codex\skills\edge-bitwarden-access\scripts\edge_bitwarden_helper.ps1 -Action Status
   ```

2. Verify Bitwarden CLI access:
   ```powershell
   $bw = 'C:\Users\paul\AppData\Local\Microsoft\WinGet\Packages\Bitwarden.CLI_Microsoft.Winget.Source_8wekyb3d8bbwe\bw.exe'
   $env:BW_SESSION = (Get-Content -Raw -LiteralPath "$env:TEMP\bw-session.txt").Trim()
   & $bw status --session $env:BW_SESSION
   ```

   Continue only if status is `unlocked`. If no usable session exists, first try the durable Windows Credential Manager unlock:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\paul\.codex\skills\edge-bitwarden-access\scripts\edge_bitwarden_helper.ps1 -Action UnlockFromCredentialManager
   ```

   If the master password has not been stored yet, ask the user to run this once in their own PowerShell. Do not run it through an automation log if it might prompt for input invisibly:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\paul\.codex\skills\edge-bitwarden-access\scripts\edge_bitwarden_helper.ps1 -Action StoreMasterPassword
   ```

   After that, retry `UnlockFromCredentialManager`. If the user does not want Credential Manager storage and Bitwarden is already logged in but locked, ask the user to run:
   ```powershell
   $bw = 'C:\Users\paul\AppData\Local\Microsoft\WinGet\Packages\Bitwarden.CLI_Microsoft.Winget.Source_8wekyb3d8bbwe\bw.exe'
   $session = & $bw unlock --raw
   Set-Content -LiteralPath "$env:TEMP\bw-session.txt" -Value $session -NoNewline
   ```

   If Bitwarden is logged out, use `bw login --raw` instead.

3. Launch the correct Edge profile with a fresh debug port:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\paul\.codex\skills\edge-bitwarden-access\scripts\edge_bitwarden_helper.ps1 -Action Launch -Url "https://example.com/login" -Port 9224
   ```

   Prefer `--profile-directory="Default"` and avoid `--user-data-dir`. If `--user-data-dir` is unavoidable, quote the entire `User Data` path inside one argument; otherwise Edge may open `data/` in a throwaway profile.

4. Connect to Edge with Playwright over CDP:
   ```js
   const { chromium } = await import("playwright");
   const browser = await chromium.connectOverCDP("http://127.0.0.1:9224");
   const context = browser.contexts()[0];
   const page = context.pages().find((p) => /target-site/i.test(p.url())) ?? context.pages()[0];
   ```

5. Find the Bitwarden item without exposing secrets:
   ```powershell
   & $bw sync --session $env:BW_SESSION
   $items = (& $bw list items --search "site-name" --session $env:BW_SESSION | ConvertFrom-Json)
   $items | ForEach-Object {
     [pscustomobject]@{
       Id = $_.id
       Name = $_.name
       Username = $_.login.username
       Uris = (@($_.login.uris | ForEach-Object { $_.uri }) -join '; ')
       RevisionDate = $_.revisionDate
     }
   }
   ```

   If the direct search misses, list items and filter by name and URI. Show only sanitized metadata, then select the best item.
   Always sync before concluding an item is missing; the browser extension can show items that the CLI does not see until after `bw sync`.

6. Retrieve the selected item and fill the form in memory only. For an explicitly authorized account-creation or credential-change workflow, generate a strong site-compliant password, save it to the correct Bitwarden login, and use that saved password for the form without displaying it:
   ```js
   const item = JSON.parse(itemJson);
   await page.locator('input[type="email"], input[type="text"]').first().fill(item.login.username);
   await page.locator('input[type="password"]').first().fill(item.login.password);
   ```

7. Submit and verify when Paul explicitly authorized the account or credential action and the active browser-control policy permits it. If the site requests OTP/TOTP and the selected Bitwarden item can generate it, retrieve the current code with Bitwarden CLI, fill it in memory only, submit it, and do not print or retain the code. If Bitwarden cannot generate the code, ask the user for it. For CAPTCHA, attempt the verification under the task authorization and notify Paul if unsuccessful. If the site requests SMS, email, push approval, or another user-only live action, pause for the user.

   AA.com exception: when American Airlines offers verification by SMS or email to `paul@informedcio.com`, choose the email option first. Use Paul's local `C:\git\webaccess\webaccess.ps1` Microsoft Graph access for `paul@informedcio.com` to find the AA verification email and extract the code. Do not use the Outlook connectors for this mailbox. Ask Paul only if Graph access is unhealthy, the code email does not arrive, or AA requires SMS/push instead. Attempt CAPTCHA under the task-authorization rule and notify Paul only if unsuccessful.

8. Cleanup:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\paul\.codex\skills\edge-bitwarden-access\scripts\edge_bitwarden_helper.ps1 -Action CleanupSession
   ```

   `CleanupSession` removes only the temporary `bw-session.txt`; it does not remove the durable Credential Manager entry. To remove the stored Bitwarden master password, run:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\paul\.codex\skills\edge-bitwarden-access\scripts\edge_bitwarden_helper.ps1 -Action DeleteMasterPassword
   ```

## Edge Launch Notes

- If a normal Edge window is already running, a new `--remote-debugging-port` may not attach. Close only the relevant Work-profile login window if needed; leave unrelated windows alone.
- Confirm the reachable CDP endpoint with:
  `Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:9224/json/list'`
- If port `9223` returns only Monarch or `Downloads`, it is the wrong profile for personal site logins.
- A correct Marriott-style result looked like a window title ending in:
  `- Work - Microsoft Edge`

## Safety Notes

- The Bitwarden account is AI-specific, but its contents are still sensitive. Keep them out of chat and logs.
- Storing the Bitwarden master password in Windows Credential Manager is a user-authorized local convenience tradeoff: it avoids plaintext Codex files and command-line passwords, but any process running as the same Windows user that can call Windows Credential Manager and knows the target name may be able to retrieve it.
- Do not inspect browser cookies, local storage, profile databases, or saved browser passwords.
- Do not accept Bitwarden extension permission prompts through automation unless the user explicitly asks for that permission action.
- Do not leave `bw-session.txt` behind unless the user is actively continuing the login workflow.

## Account Change Follow-Through

If the workflow changes an account email or username on a site, verify the saved site profile after the final MFA step, then update the matching Bitwarden item username only after the site confirms the change. Clean up the temporary Bitwarden session afterward, but do not remove the durable Credential Manager master-password entry unless Paul asks.

## Familiar-Site Notices

Paul prefers automatic click-through of routine "I agree" notices on websites Codex has previously worked with during an authorized task. Use session or saved workflow evidence to establish familiarity. Apply this preference to non-binding acknowledgments and routine notices when no higher-priority confirmation is required. Familiarity does not override browser/tool requirements for action-time confirmation of legally binding terms, new commitments, expanded permissions, or other protected actions. When such confirmation is required, explain the concrete requirement briefly and use any specific approval already provided for the current action.
