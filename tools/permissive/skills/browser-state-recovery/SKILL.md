---
name: browser-state-recovery
description: Recover from ignored clicks, loading states, virtual-list scrolling and stale selectors during browser work in Codex Permissive. Use when interaction fails to produce the expected page state.
---

Use the chat's selected browser/profile and the browser MCP operations. Inspect before choosing controls. Prefer click/type/scroll over synthetic events; evaluate is useful for bounded read-only DOM facts, not clicks or hidden application APIs.

Define the expected state change before acting: active tab, new URL, selected row or reader identity. Verify that change after the action; command success is insufficient. Loading and an empty virtual list are distinct states. Wait for actual rows or the expected reader, then inspect. A scrollport may be inside the listbox; browser scroll can locate it. Reinspect after scrolling because a recycled DOM node can represent a different item.

If state is unchanged, take a fresh narrow snapshot, choose a different observed selector or wait for readiness, and retry. Allow at most two unsuccessful recovery attempts for the same transition. Then report observed state and the blocker. Do not repeat ineffective scripts or rewrite installation, credentials or native-host configuration during unrelated browser work.
