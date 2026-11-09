# Recovery with observable progress

For each transition define expected evidence before acting. Refresh after acting. If loading is explicit, use one bounded readiness wait for the expected control, then inspect again. If no progress, inspect the blocker, take one alternate observed route, and verify. At most two unsuccessful recovery attempts for the same transition; then stop and report the exact observed limitation. Do not reload or change profiles as routine recovery.

| Complication | Recovery | Success / stopping evidence |
|---|---|---|
| Collapsed navigation pane | Click observed Show navigation pane; expand required group/root disclosure only | expected tree appears; expanded ancestor plus selected child after opening |
| Unexpected/restored search scope | Reconfirm mailbox folder, open scope menu, choose explicit observed scope; inspect chips/refiners/advanced fields, clear conflicts and submit | correct folder in filter label/selected scope plus new Results; never broaden silently |
| Clear filters leaves chips | Pending edits have not been submitted; inspect fields and submit Search, then inspect chips/text again | cleared query/filter state in results; Exit search is safer when starting afresh |
| Search suggestions cover list | Inspect expanded search combo/status/listbox; close suggestions using supported control or explicit Search submission; refresh before row click | no suggestion overlay, intended option selected, matching reader; remove any unintended chip and resubmit |
| Reader shows previous message | Compare current row selection to individual reader sender/subject/displayed date; wait for reader; retry a fresh observed row target once | all relevant identifiers agree; URL alone can retain an old item suffix |
| Relevant conversation message collapsed | Inspect message containers, counts and header disclosure; expand only relevant individual item | independent From/To/date/body visible for that item, not quoted headers |
| Dialog/account/profile card hides controls | Read dialog, use its own Close or verified toggle; refresh. Account control label may change from display name to Account manager for … | intended underlying controls regain semantics; do not click Sign out/account switching |
| Unsaved-work/discard dialog | Preserve user draft/edit; use keep-editing/safe close only if exposed and known to retain work | if leaving would discard, request user interaction and stop dependent navigation |
| Loading/transient failure | Distinguish Searching/progressbar from explicit no-match/empty. Bounded wait for list/reader then snapshot; use visible retry only if no user work at risk | loading settles into rows/explicit empty, or report failure; no arbitrary sleeps |
| Stale element or changed label | New snapshot; rederive role/name/owner; numeric indices and recycled selectors cannot be reused blindly | correct state/identity after retargeting; do not repeat stale click |
| Semantic target covered/not hittable | Inspect current screenshot and overlay first. CUA may scroll/click current unobscured pane coordinates. Native adapter needs an observed CSS target | correct pane moved/target changed, not page or ribbon |
| Virtual list stops progressing | Ledger first/last identities and scroll metrics; identify actual scrollport (possibly list child); resolve loading/collapsed group/load more; try one correct pane route | new identities/position; stable repeated rows without settled terminal boundary => partial/stalled |
| Date picker leaves overlay open | Confirm chosen date/range; close picker with current date heading/toggle | intended range and event targets visible; scope duplicated date controls to picker |
| View switch resets date | Inspect pressed view and range; renavigate date after changing view | both view and requested interval agree |
| Event details show editable fields | Do not edit or use toolbar Cancel/Save/Send. Locate event header Close, distinct from Tracking Close | unchanged event closes; unsaved warning requires preservation/handoff |
| Applied calendar filter needs clearing | Capture active criteria in all relevant nested menus before clearing; checked default Appointments alone is insufficient | restore exact captured criteria afterward; if original criteria were not captured, do not guess and disclose cleared-state limitation |
| Expired session/account chooser | Record browser/tab; request user to complete sign-in/MFA in the existing session; do not inspect authentication material | resume only after new observation and verified account/mailbox; no automatic profile switch |

## Completion and coverage

Folder empty requires correct selected hierarchy, applicable filters/tabs, settled load and explicit empty state; an unrendered list is insufficient. Search no matches requires submitted query, verified scope/filters and settled no-match text. Available search results are exhausted only when traversal began at top, all groups/pages/load-more controls are covered, and a settled terminal boundary yields no new identities after bounded readiness checks. A server cap or “more results available” prevents a mailbox-wide completeness claim.

With CUA, bounded read-only evaluation may inspect scrollTop/clientHeight/scrollHeight on DOM-derived scrollports; do not call scrollTo/click/change values through evaluate. Native `scroll` may return position; absent terminal evidence, report “inspected visible and subsequently loaded rows; full result set not established.” Read only what the user needs; navigation validation does not require reading every search result.

For calendar absence confirm selected calendars, actual date/range, filters, load announcement, all-day band, time-grid scroll and Month overflow. Report scope-specific absence; it does not apply to unchecked calendars or dates omitted by Work week.
