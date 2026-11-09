# Email recipes from the live interface

Read browser setup for the active adapter. These labels are verified for English organizational Outlook; rediscover in other layouts. Examples below contain generic placeholders and are not executable until replaced from observed UI/task context.

## Folder and scope

With CUA DOM, owner scoping avoids duplicate folders:

```javascript
await outlook.playwright.getByRole('group', {name: verifiedMailboxLabel, exact:true})
  .getByRole('treeitem', {name: /^Sent Items(?: selected)?$/}).click();
nodeRepl.write(await outlook.playwright.domSnapshot());
```

Unread counts alter folder names; build a regex from the observed name, anchored to avoid another folder. Verify selected item inside that owner and Sent Items heading/list. Folder path includes mailbox and ancestors. Root disclosure may have an unnamed button; if DOM locator fails, current AX can expose Expand/Collapse secondary action. Expansion can show a progressbar before children appear.

Click the mail search combo, then inspect top scope button. Scope menu exposed menuitemradio All folders/Current folder/Subfolders and Favorites submenu. This is a clickable alternative to the advanced native select. Current folder is relative to the **verified folder at search preparation**, not a presumed Inbox. After choosing, refresh and verify the filter button labels the intended folder. Do not treat All folders as proof archive/shared mailbox inclusion; search those roots separately if needed.

## Visible filter preparation

Search filters button is labelled `Search filters, <folder/scope>, <N> other filters applied`. Its number was unreliable as a summary: values remained in advanced fields despite a zero badge. Reopen fields to verify interpretation.

Observed fields: Search in (select options All folders/Current folder/Subfolders), From/To/Cc (labelled contenteditable people controls), Subject/Keywords (textboxes), Start date of search/End date of search (read-only combo inputs), Read status (All/Read/Unread), Attachments checkbox, Add filters, Search and Clear filters.

With CUA, `getByLabel('From',{exact:true}).fill(verifiedSenderAddress)` and To counterpart produced chips that could resolve from literal addresses into display names. Inspect the resolution; confirm address from contact/header UI when ambiguity matters. Native Permissive attached `type` does not safely fill these contenteditable controls; use a supported alternate or user interaction, not synthetic DOM mutation.

Use Subject for subject restriction. Keywords is broad; it can include quoted content and must not be represented as current-body-only evidence. Add filters opens a dialog exposing Bcc, Body, Flagged, Importance, Category and other fields. Check Body and Save to expose Body textbox. Preserve previous filter-visibility configuration if temporarily changed. Save here saves filter configuration, not a message; verify the dialog context before clicking any Save.

Phrase input can tokenize according to the interface. Validate exact contiguous phrases against the individual current body/subject, even if the filter returns matches from quotes or matching words. Recipient constraints require actual address evidence; deriving an address from a display name produced a false no-match during testing. A verified recipient address corrected that search.

Clear filters cleared pending advanced values but old chips/text persisted until Search was submitted. When starting over, Exit search, reopen the intended folder, then inspect the fresh query and filters; do not rely on implicit reset.

## Date controls and syntax

Start/end inputs were readonly; `fill` failed and DOM inspection confirmed readonly. Click the combo to open dialog Calendar; scope day selection to that dialog. Fully labelled buttons have form `DAY, MONTH, YEAR`, with month/year navigation controls. Check the picker month/year, choose start, refresh and verify displayed value; repeat for end. Do not click bare day number or fill a read-only field. A date picker and Calendar app are separate surfaces.

After Search, results appeared. Reopening advanced fields confirmed the chosen start/end persisted. Use explicit inclusive calendar dates as the task's UI interval and inspect boundary candidates; UI validation did not establish minute-level/inclusive backend semantics for every case. Do not rename its generic Date control to sent/received.

Typed candidate syntax `from:alex@example.test received:DATE_START..DATE_END` was probed with actual task-local values. It returned candidates but advanced fields did not show a date range, and the date expression appeared in Keywords. This is **not verified date-operator acceptance**. Do not use it as a date restriction. No reusable syntax operator is certified by this validation; prefer visible filters. If future UI accepts a syntax expression, require interpreted control/chip evidence and matching individual-header checks, not merely Results.

## Submission, inspection and scrolling

Observe Search button after typing/filtering. Click the appropriate Search in the opened filter context; when two buttons share the label, scope to the overlay or inspect current AX/screenshot rather than select arbitrarily. Search can show Searching... before listbox options. Verify Results and actual scope/chips/filter values after loading. Close search suggestions before selecting a result; an expanded suggestion list can intercept an intended list interaction and add a people chip.

Listbox options expose sender/subject/date/preview, folder and attachment/read indicators. Sent Items rows can show recipients instead of sender. The reading pane is the authority for From. Same-subject options are not necessarily grouped conversations. Inspect sort via Sorted: By Date menu when present; search Results may expose different grouping/relevance. Do not change sorting unless the task needs it.

Scroll the result list with overlapping viewports. In validation an AX list scroll was not hittable; a current screenshot allowed scrolling the center list coordinates, exposing new older rows while the reader stayed on the selected message. Reading-pane scroll is separate. Track stable DOM/AX row identities when exposed; use composite evidence when not. Node identity can recycle and subjects can repeat. The validation scrolled past initial rows but did not exhaust that entire search.

Open an option, then verify row selected plus matching individual From/subject/date/body. URL can retain a previous item suffix after folder change and is not proof of selection. Reader may initially show loading/Nothing is selected. Inspect before one retargeted retry.

Read-state check: an unread option initially exposed Mark as read. The first semantic click left the old reader visible; a fresh screenshot and current row click opened the intended reader. During the first bounded follow-up the row still exposed Unread/Mark as read. After switching to Calendar and returning, the same row had lost Unread and exposed Mark as unread, and the folder unread count decreased by one. **An automatic read-state change was observed.** The test does not distinguish a delay from a selection/view-change trigger. Observe and disclose changes; do not deliberately toggle read state during navigation validation.

`+N others` expanded individual To recipients; Cc has its own expansion. Sender/recipient buttons open Profile Card, initially loading and later exposing contact email/action labels. Read address; do not click Send an email, Call, Chat, Copy, or profile mail deep-links merely to establish it. Close within Profile Card. Contact-card “recent messages/files” are broader unrelated data, not the selected message's attachments.

Conversation message header/disclosure must expose independent headers/body after expansion. Quoted From/Sent/To/Cc sections and Re: subjects alone do not establish independent messages. Check later actual messages separately. The current header timestamp is labelled only as Outlook displays it; quoted sent/date lines can use other zones/formatting and describe earlier correspondence.

## Attachments and return

Inspect the intended message's attachment collection, displayed names/types/count and any additional-items control. Attachment options can expose Preview, Open, Save or Download; use preview only when present and relevant. Refresh after opening a menu/preview; close it with its own Back/Close. Do not download to validate a skill. Attachment collection/menu was exercised; preview rendering was not exercised.

Exit search/Close search returns to the underlying folder in tested layout. Verify selected owner path, folder heading or Focused/Other, search field/chips closed and actual contents. To retain search while reading multiple candidates, close only message/contact/attachment overlay, then reconfirm Results/scope/query. Do not press the global Exit search accidentally.
