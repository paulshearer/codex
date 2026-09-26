# Content refusal audit

## Scope and baseline

The requested scope was content/task refusal behavior in the local open-source harness, at `rust-v0.157.1`, commit `36650394c5b38c2990ccf2a3457165ca3e9d9726`. Action approvals, Guardian, execution policies, and sandbox controls were outside that scope.

## Finding

The inspected bundled prompts contain no general local content/task refusal gate that could be removed as a justified source patch. The model decides what content to return. The relevant transport parser handles provider response events and failures; deleting that reporting would hide or misrepresent a provider decision.

The custom model catalog therefore embeds the upstream prompt unchanged. The companion retains refusal/error data from providers and preserves native action controls. There is no content-refusal removal patch in this branch.

## Inspected sources

- [Bundled model prompt](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/models-manager/prompt.md)
- [Responses event parsing](https://github.com/openai/codex/blob/rust-v0.157.1/codex-rs/codex-api/src/sse/responses.rs#L429)
- Model prompt selection, model catalog metadata, and tool routing in this same pinned source tree.

This is a scoped source finding. It does not claim that every provider, model, or closed desktop component has identical behavior.
