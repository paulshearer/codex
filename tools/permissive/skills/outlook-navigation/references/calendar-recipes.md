# Calendar recipes from the live interface

Read browser setup for supported actions. Observed labels are English organizational Outlook; scope duplicated controls and refresh after each transition.

## Calendars and views

Mail/Calendar are DOM rail buttons, AX checkboxes. Click Calendar and verify pressed state and calendar main region. Side navigation initially showed only selected Calendar under My calendars. Show all exposed unselected personal holiday/birthday calendars, team calendars and group calendars; it did not select them. Show selected restored abbreviated list. Inspect `[selected]` options within the owner listbox; do not assume every listed calendar is active. A group expansion and calendar selection are different operations.

Default personal Calendar can be corroborated by My calendars ownership and full-event containing-calendar label. If several same-name personal calendars exist, inspect explicit default/ownership markers or ask for the intended calendar. Do not infer default merely from which is displayed. To change selection use the actual exposed checkbox/option control and recheck the resulting set/count; this selection change path was not exercised in validation.

Home ribbon had Day/Work week/Week/Month buttons with pressed state, plus Split view disabled when only one calendar was selected. The four primary views were tested. Do not invent Agenda as a full view: Month overflow exposed an agenda list separately. Additional Day options may exist; inspect them if needed.

Filter applied opened a menu. Appointments was checked both initially and after Clear filters; that check is not evidence of which nondefault criterion was active. Clear filters yielded Filter without applied indicator and disabled Clear filters. The original nested criterion was not captured, so this filter change could not be fully restored; validation disclosed that limitation. Capture all nested criteria before clearing. Hidden filters can exclude meetings even when the grid appears empty; inspect checked criteria and submenus before clearing or asserting absence.

## Dates and time zone

Previous/next buttons include destination day/week/month labels; they change with the active view. Use Today labelled with the current date and verify the resulting range. A toolbar heading `DATE_OR_RANGE, Jump to a specific date or date range.` opens a menu containing a month grid, year/month grid and previous/next controls. The sidebar mini-month contains duplicate dates; scope to the menu or navigation owner you deliberately opened. Picker date selection changed the view but left the menu open; toggle the now-updated heading to close it.

For a distant date, use the exposed month/year grid/navigation, refresh after each step and stop at the specified date; do not run a blind click loop. Full range selection/remote-year traversal was not tested. For a multi-day task use an exposed suitable view and check its actual range, traversing additional periods as needed. Work week excludes weekends. Changing view can reset the anchor date, so choose view first, date second, and verify both.

Settings opened a dialog, initially loading. Verified path: Calendar tab > View sub-tab > Time zones group > Display my calendar in time zone. Read selected display value only. The field's semantic value was redacted, but screenshot exposed it. The live label was a named Eastern Time zone with a UTC offset in the label; it was read, not changed. Do not hardcode it for future accounts or treat that label offset as a fixed year-round UTC conversion. Close settings through Close settings.

## Event reading

Grid event button name exposed subject, start/end dates/times, “By” person, show-as and recurrence flags (Recurring event / Exception to recurring event). A canceled title appeared among loaded events. These are navigation evidence; full details distinguish organizer from owner and response from show-as status.

Click an existing event, refresh until Title/Time and View event appear. Compact preview can omit attendees/calendar/zone. View event expands details; do not use Join/Chat/Recap as a details expansion. In full details the header named the event and Calendar; tracking showed Organizer and Attendees grouped by response. An invitation “Sent on” timestamp in Tracking is not the event start time.

Organizer View event opened title/attendee fields and the notice “You're editing one event in the series,” with Edit series and Edit this and all following events. This established occurrence/exception context without making any edit. Toolbar Cancel is cancel-meeting, not a close affordance. Read-only navigation must use the event header X/Close. Another Close controls Tracking. Use current DOM structure or screenshot to distinguish them; `.first()` is not a general close recipe.

Attendee View event exposed subject, attendee collection, start/end, meeting location/link and Tracking response information alongside Accept/Decline/Follow/RSVP/Propose new time/Delete controls. Read without responding. No recurrence banner alone does not prove a one-off event; report recurrence not exposed unless the grid/details state supports it. For all-day, require an explicit All day control/label or an unambiguous all-day-band event; midnight time or a long multi-day event is insufficient. No explicit all-day event was tested.

The containing calendar must come from event header or explicit field, not merely selected calendars. A shared event may expose limited attendees/details; report unavailable fields. Do not use organizer identity or body text as proof of containing calendar.

Close event header and verify the previous range/view/calendars. If a discard/save dialog appears, preserve existing edits; never choose discard automatically. Full-event opening without editing returned without such a dialog in validation.

## Hidden events and absence

Day exposed a time-grid main plus load announcement. Weekend zero-events view was verified for the one selected calendar after clearing filters; that cannot be generalized to unchecked calendars. Day/Week can contain all-day events above the time grid and events outside the current vertical viewport.

Month had +N more events from a labelled date in agenda view, even when no individual event title appeared in the day cell. Opening it exposed that day's agenda with subject/time/duration/recurrence. Inspect all rows and scroll its own container when necessary; collapsed/overflow events prevent an absence claim. A total Loaded count describes the loaded range, not necessarily current readable rows or inspected events.

Return to the checkpoint by closing event/picker/filter overlays, restoring temporary view/date/filter/calendar sidebar settings as needed, then Mail and its verified folder. Do not assume the app switch restores the intended folder until selected path/list are observed.
