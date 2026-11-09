# Browser setup and supported adapters

Read the documentation of the tools actually exposed to this task. Do not assume a Playwright installation, import a browser package, attach through another protocol, or use shell/network requests to Outlook. Discovery and interaction stay within the supported browser tools. No private Outlook APIs, storage/cookies/tokens/credentials, app internals, or hidden state.

## Context checkpoint

Inventory existing tabs by observed URL/title. Bind the intended Outlook tab in its existing browser/profile, inspect it, and retain that binding. If several Outlook tabs exist, inspect their state before choosing; prefer the relevant existing session. Do not replace tabs or reload merely to obtain a familiar screen.

Record only in task memory: selected browser/tab; surface and language; signed-in address from visible account control; active mailbox root; Mail/Calendar pressed state; selected folder path or selected calendar set; current view/range; search query/chips/pending and active filters; selected message/event; overlays and drafts. An account chooser listing other saved identities does not mean those mailboxes are open. Folder root and signed-in account can differ when a delegated mailbox is open. Resolve mismatches with the user's requested context before reading.

Preserve drafts, detached compose windows and existing event edits. Dismiss only read/navigation overlays through an observed close/toggle. Do not discard unfinished work to perform validation. Authentication requires user interaction; stop before credential/MFA entry and resume from a fresh inventory/state afterward.

## Live-validated adapter: cua_repl

Validation on 2026-10-10 used the connected Edge extension through `mcp__cua_repl.js`. Start a fresh REPL with exactly one entry call, then read its returned documentation:

```javascript
await cua.getState();
```

In a later call, bind the observed tab and browser IDs; never reuse the validation IDs:

```javascript
var outlook = await cua.getTab(observedTabId, { browser: observedBrowserId });
```

Returned browser documentation exposes both AX/native wrapper and supported `tab.playwright` APIs. Use DOM semantics consistently; AX can represent an app-rail control as checkbox while DOM exposes button.

```javascript
// Labels below were observed in English organizational Outlook.
await outlook.playwright.getByRole('navigation', {name:'left-rail-appbar', exact:true})
  .getByRole('button', {name:'Mail', exact:true}).click();
nodeRepl.write(await outlook.playwright.domSnapshot());
```

Use `getByRole`, `getByLabel`, exact labels, owner-group scoping and current row identity. Do not use `.first()` merely to suppress ambiguity. `count`, `isVisible`, `getAttribute`, `innerText` and bounded read-only `evaluate`/`evaluateAll` can inspect exposed DOM attributes/scroll metrics. Do not mutate the DOM through evaluate.

Supported actions used: locator `click`, `fill`, `selectOption`, `check`/`uncheck`; `domSnapshot`; `getAXState`, `getScreenshot`, `scroll`; `performSecondaryAction` only when that action is explicitly exposed. AX indices are ephemeral; derive them from the newest state. After a screenshot get a new AX state before relying on numeric indices. Coordinates require a fresh screenshot and apply only to that current tab view; never copy example coordinates. State/screenshot wrapper methods emit themselves; do not duplicate their output. `domSnapshot` is returned as text and can be emitted with `nodeRepl.write`.

Use `locator.waitFor({state:'visible', timeoutMs:5000})` for a known expected control, then inspect loading state. A bounded observation immediately after a transition can legitimately show loading; refresh once when loading is present. Do not repeat unchanged observations indefinitely or use long sleeps. A redacted field requires screenshot inspection, not hidden-state probing.

## Codex Permissive native browser adapter

The local repository's `browser/worker.mjs` and `browser/extension/inspection.js` were inspected for compatibility. This adapter was **not exposed as a direct browser tool in the validation chat**; its Outlook interactions are not live-validated here. Prefer the native browser tool when a future Permissive task exposes it, and read its current schema. Do not send a task to another agent merely to obtain browser access.

The inspected schema accepts `operation`, `pageId` (dedicated) or `tabId` plus `connectionId` (attached), `selector`, `text`, `script`, `offset`, `limit`, `direction`, `url`, `file`. Supported relevant operations: `list`, `inspect`, `click`, `type`, `scroll`, `wait`, `screenshot`, `evaluate`. Example arguments to the discovered browser tool (no assumed tool name):

```json
{"operation":"list"}
{"operation":"inspect","tabId":123,"connectionId":"OBSERVED_CONNECTION","limit":8000}
{"operation":"click","tabId":123,"connectionId":"OBSERVED_CONNECTION","selector":"OBSERVED_SELECTOR"}
{"operation":"scroll","tabId":123,"connectionId":"OBSERVED_CONNECTION","selector":"OBSERVED_LIST_SELECTOR","direction":"down"}
```

Replace identifiers with the listing result. Dedicated mode instead uses `pageId`. Select the configured browser/profile; never route to an arbitrary connected profile. Attached listing can paginate connections; follow `nextOffset`. Inspection returns title/URL, text with `nextOffset`/`totalCharacters`, and up to 80 actionable elements with role/name/selectors. Inspect the search/folder/list/reader region narrowly if ribbons crowd controls out. Text `offset` pagination is **not** scrolling or exhaustion of the virtual message list.

Prefer CSS built from observed role/aria-label and owning hierarchy when unique; use returned `data-permissive-ref` selectors for the freshly inspected target. Reinspect before clicking a recycled row. Names can be truncated at 400 characters; read the current row/reader to verify identity. Native attached `click` uses CSS and native browser input; do not invent `getByRole` methods, coordinate/key fields, `selectOption`, or Playwright calls for this adapter.

`scroll` accepts up/down/top/bottom and searches a selected list's scrollable child/ancestor; inspect returned position and new row identities. `wait` is bounded internally (30 seconds in inspected implementation), but in attached mode it establishes selector presence, not settled content or absence of loading. Its schema has no custom timeout field. Verify readiness after it and stop after two failed recovery attempts.

`type` targets fillable inputs/textareas. The inspected attached implementation does not safely support contenteditable people fields, native select editing, or read-only date fields. Use clickable picker/menu controls, validated textual syntax when the current interface confirms interpretation, or request user interaction for the unsupported control. Do not inject synthetic events or mutate select/contenteditable through evaluate to work around these limits. Native `evaluate` is allowed here only for bounded visible DOM facts. Native screenshot helps choose an observed CSS target; it does not add coordinate-click support. Native dedicated `release` closes its page; do not release a preserved Outlook session as cleanup.

## Keeping the suite usable

Reference links are relative to the three sibling skill folders. Install/copy all three together. Codex Permissive injects explicitly assigned SKILL.md files, not a general automatic catalog; select these files in the workspace/chat Skills control. Do not replace unrelated assigned skills or browser/profile settings. Exporting a single skill folder without the navigation references breaks shared links; include the complete suite or copy the referenced resources into a self-contained export.
