---
name: bitwarden-vendor-card-payment
description: Pay vendor bills, invoices, tolls, portal balances, or one-time charges using a credit card stored in Paul's Bitwarden vault. Use when Codex needs to find card details with Bitwarden CLI, sync stale vault data, fill a vendor payment form, submit an authorized payment, create/store a vendor account only when needed, verify the receipt or cleared balance, and update task-tracker when requested.
---

# Bitwarden Vendor Card Payment

## Rules

- Use the Bitwarden CLI, not the Bitwarden browser extension UI, as the source of card data.
- For statements, bills, invoices, and payment notices, automatically use the `hledger` skill to check the active `C:\hledger` ledger for prior payments before treating the item as outstanding or retrieving card details. Match amounts, vendor/account, and relevant dates; include fee-adjusted or split payments when supported by evidence. Check ledger freshness. Explain existing-payment evidence and avoid duplicate payment; a missing or ambiguous ledger match still requires a live balance/payment-history check when available.
- Never print, paste into chat, save to files, or log full card numbers, CVV/CVC, passwords, session tokens, item JSON, cookies, or local storage.
- Keep retrieved card data in process memory only. Show only sanitized metadata such as item name, brand, last four, expiration month/year, and revision date.
- Submit a payment only when the user has asked to pay that vendor/bill and the payment page matches the expected vendor, invoice/account context, and amount/current balance.
- If the amount differs from the source document, verify that the site ties it to the same invoice/account. Proceed only when the difference is clearly a current balance, fee, or penalty for that same bill; otherwise ask Paul.
- Do not create a vendor account unless it is required to pay or Paul explicitly asks. If an account is created, store the new login in Bitwarden before finishing.
- Leave "save this payment method" unchecked unless Paul explicitly asks to store the card with the vendor.
- Stop and ask Paul for MFA/OTP, CAPTCHA, unexpected account changes, recurring autopay enrollment, subscription changes, or any payment above the authorized/current balance.
- Clean up `$env:TEMP\bw-session.txt` before finishing unless Paul is actively continuing the workflow.

## Workflow

1. Confirm the payment target:
   - Vendor or portal URL.
   - Invoice/account/reference number.
   - Expected amount or current balance.
   - Due date and payer identity when available.

2. Unlock Bitwarden with the local helper:
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\paul\.codex\skills\edge-bitwarden-access\scripts\edge_bitwarden_helper.ps1 -Action Status
   powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\paul\.codex\skills\edge-bitwarden-access\scripts\edge_bitwarden_helper.ps1 -Action UnlockFromCredentialManager
   ```

3. Verify the CLI session and sync before searching:
   ```powershell
   $bw = 'C:\Users\paul\AppData\Local\Microsoft\WinGet\Packages\Bitwarden.CLI_Microsoft.Winget.Source_8wekyb3d8bbwe\bw.exe'
   $env:BW_SESSION = (Get-Content -Raw -LiteralPath "$env:TEMP\bw-session.txt").Trim()
   & $bw status --session $env:BW_SESSION
   & $bw sync --session $env:BW_SESSION
   ```

   Always run `bw sync` before concluding that a card is missing. The browser extension can show items that the CLI does not see until after sync.

4. Find card items with sanitized output only:
   ```powershell
   $items = (& $bw list items --session $env:BW_SESSION | ConvertFrom-Json)
   $items |
     Where-Object { $_.type -eq 3 -or $_.card -or $_.name -match 'card|visa|master|amex|marriott|bonvoy|citi|chase|capital|discover' } |
     ForEach-Object {
       [pscustomobject]@{
         Id = $_.id
         Name = $_.name
         Type = $_.type
         Brand = $_.card.brand
         Last4 = if ($_.card.number) { $_.card.number.Substring([Math]::Max(0, $_.card.number.Length - 4)) } else { '' }
         ExpMonth = $_.card.expMonth
         ExpYear = $_.card.expYear
         RevisionDate = $_.revisionDate
       }
     }
   ```

   Prefer the card Paul names. If he says "Marriott Bonvoy", use the `Marriott Bonvoy` card item, not a similarly named login item.

5. Retrieve the selected card only when ready to fill the payment form:
   ```powershell
   $itemJson = & $bw get item '<item-id>' --session $env:BW_SESSION
   ```

   Do not print `$itemJson`. Parse it inside the automation process and use `item.card.number`, `item.card.code`, `item.card.expMonth`, `item.card.expYear`, and `item.card.cardholderName`.

6. Fill the vendor payment form:
   - Use the vendor's guest-payment flow when available unless Paul asked to log in.
   - Use the invoice mailing address as billing address if the card item does not specify a billing address and no better source is available.
   - For tokenized card iframes such as CardConnect, fill the card number in the iframe and the CVV/month/year fields in the parent page as required.
   - Keep payment method save/autopay/marketing opt-ins off unless requested.

7. Confirm before submitting:
   - Verify visible confirmation fields show the expected/current amount.
   - Verify payment method metadata, such as `Visa ending in 7887`, matches the selected card.
   - Verify invoice/account/vendor context remains correct after navigation.

8. Submit once and capture the result:
   - Record confirmation number, receipt number, paid amount, date/time, payment method last four, and any post-payment balance.
   - Run a fresh lookup or refresh when possible to verify the balance is zero or the invoice is paid.
   - Do not retry a payment after an ambiguous timeout without first checking payment history/balance.

9. If task-tracker is requested:
   - Use `C:\git\task tracker\tasks.csv` as the source of truth.
   - Mark the task `completed` only after payment confirmation or a cleared balance is verified.
   - Include the confirmation number and verification result in `notes`; do not include full card data.

10. Cleanup:
    ```powershell
    powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\paul\.codex\skills\edge-bitwarden-access\scripts\edge_bitwarden_helper.ps1 -Action CleanupSession
    ```

## Bill-From-Photo Triage

When the source is a photographed toll, tax, medical, utility, or vendor bill:

1. Extract all identifiers before visiting the portal:
   - invoice, notice, statement, bill, account, plate, VIN, payer, patient, ZIP, due date, and amount.
   - Crop or zoom unclear fields rather than guessing.
2. Check whether the bill is already paid before retrieving card data:
   - Search local ledger/raw imports for exact or fee-adjusted amounts when relevant.
   - Use the official vendor portal or a collections portal reached from the official page.
   - Use "all payments" or paid/unpaid status filters when the portal supports them.
3. Treat "not found by notice number" as inconclusive:
   - Try the portal's alternate formats, such as stripped leading zeros, owner name, plate, VIN, ZIP, or account number.
   - Cross-check by payer and item details before concluding unpaid.
4. If the current portal amount differs from the paper amount, proceed only when the site clearly ties it to the same account/current balance, including late fees, service fees, or collection fees.
5. For WebForms or old government portals:
   - Respect postback timing; verify form fields immediately before clicking Search or Pay.
   - If headless payment is rejected, retry in the Work Edge profile only after a fresh unpaid-status check.
6. Print/save receipts whenever possible:
   - Prefer a portal receipt PDF.
   - If the receipt token expires, use captured confirmation text plus a fresh paid-status lookup.
   - Store receipt path and confirmation details in task-tracker when requested.

## Common Failure Modes

- **CLI card list is missing a card visible in the Bitwarden extension**: run `bw sync --session $env:BW_SESSION`, then list items again.
- **Debug port does not attach to Edge**: use installed Edge headless or close only the relevant Work-profile Edge window before relaunching with the helper.
- **Receipt opens as a blob with no extractable text**: capture the confirmation dialog text and do a fresh balance/invoice lookup.
- **Site shows a different invoice number after lookup**: verify it belongs to the same account or current balance before paying, and record both numbers in the audit note.
- **Collections redirect changes the amount**: verify the linked collections site came from the official portal, matches the same person/account/item, and shows the current balance before paying.
- **Portal search returns no result**: search by alternate official fields before deciding the bill is absent or paid.
