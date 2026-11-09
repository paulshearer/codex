# Verified interface and variations

Verified 2026-10-10: organizational Outlook on `outlook.cloud.microsoft`, English, desktop Edge viewport, expanded app rail and folder pane, full ribbon, center message list and right reading pane. Account address was confirmed through the account control and matched the primary mailbox root. The initial state was Mail / primary Inbox, a selected individual message, no active search, compose draft or modal. Drafts existed in the folder tree and were left alone. A separate existing Outlook tab was not changed.

No real addresses, display names, subjects, bodies, meeting links, attachment names, message IDs, tenant IDs, or mailbox counts are saved in these references. Use `account@example.test`, `archive mailbox`, `Projects / Renewals`, `Quarterly review`, `DATE_START`, `DATE_END`, `DATE_TARGET` as illustrative placeholders, replacing them from the task and observed UI.

## Mail map

| Element | Observed semantics | Reliable evidence |
|---|---|---|
| App rail | DOM navigation `left-rail-appbar`, buttons Mail/Calendar | pressed app and corresponding regions |
| Pane toggle | Hide navigation pane / Show navigation pane | folder tree appears/disappears |
| Folder pane | region Navigation pane, tree, mailbox groups, treeitem levels | selected folder within the correct owner group |
| Favorites | group Favorites, expanded root, shortcuts | shortcut presence does not establish underlying mailbox |
| Primary mailbox | labelled root and child group | account verified separately; owner nesting distinguishes duplicate names |
| Archive mailbox | separate In-Place Archive root | expanded state and its child group; not the primary Archive folder |
| Folder view | complementary Message list, folder heading in ordinary folders | selected path + heading/list; Inbox may instead show Focused/Other |
| Rows | listbox Message list, option with sender/subject/date/preview | row selected + matching individual reader |
| Reader | main Reading Pane, individual heading/From/To/Cc/date, document Message body | quoted body headers are not current headers |
| Search | combobox Search for email, meetings, files and more. | Results heading, active scope and settled results/no-match state |
| Overlays | account panel, search filters, profile cards, pickers | underlying controls may become anonymous/inaccessible while covered |

The observed Mail list displayed separate messages, including several sharing a subject. That is not proof of conversation grouping. No real collapsed multi-message conversation was found/expanded in this validation. Future agents must inspect row counts/expanders and independent message containers. Do not alter grouping solely to satisfy validation.

## Calendar map

Day, Work week, Week, Month were exposed and exercised. App rail Calendar is pressed. Personal calendars appear under My calendars; other groups may appear only after Show all. `option [selected]` indicates displayed calendars. Show selected/Show all changes sidebar visibility, not which calendars are displayed. Names such as Calendar/holidays/birthdays can repeat or have suffixes. Team/group lists can have Show more calendars.

Mini-month lives in navigation `date selection, Jump to date selection`. Toolbar controls label Today and previous/next destination dates. Date/range heading opens a separate menu with month/year grids; the same date may appear in the sidebar and menu, so scope to the opened menu. Date selection can leave the menu open. Changing Work week back to Day reset the displayed date to today during validation; always reconfirm the range.

Calendar grid is a main region labelled `calendar view, current time: ...`; event buttons include subject, time/date, owner, show-as status and recurrence indicators. Loaded announcements state event count, selected-calendar count and range. Month exposed +N more events in agenda view, including days with no individual titles visible. Preview exposes Title, Time and View event. Full event header includes subject/type/calendar. Organizer View event opened an editable-looking occurrence form without any edit; attendee View event opened meeting details with response controls. Event header Close and Tracking Close are different targets. Toolbar Cancel on organizer form is a cancellation action.

## Inspect differences rather than assume them

Consumer Outlook (`outlook.live.com` commonly signals consumer context), organizational hosts such as `outlook.office.com`/`outlook.office365.com`/`outlook.cloud.microsoft`, alternate languages, compact windows, simplified ribbon, bottom/hidden reading pane, multiple mailboxes and shared calendars may expose different labels and controls. Hosts are a clue; corroborate the account/control and product surface. Consumer Outlook, other languages and mobile/compact layouts were untested. Do not substitute organizational labels or URLs blindly.

Re-map by accessible role, translated label, hierarchy and current screenshot. Navigation pane can be collapsed, Favorites absent, search scope restored, or view controls moved into a menu. A message may open full width with back/close instead of a reading pane. Calendar might expose additional views; only use a view actually observed. Existing UI IDs, dynamic suffixes, numeric AX indices, counts and coordinates are session-specific, not reusable constants.
