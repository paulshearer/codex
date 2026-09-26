# Verification record

## Source and runtime

- Native baseline: `rust-v0.157.1`, `36650394c5b38c2990ccf2a3457165ca3e9d9726`.
- Native Rust source, bundled prompts, and checkout lockfile are unchanged. The isolated release build normalizes only 155 local workspace package versions; external dependency identities and checksums remain unchanged.
- Windows build: Rust 1.95.0, x86_64 MSVC, upstream release flags and checksum-pinned V8 artifacts.
- Portable Python: official embeddable 3.13.7. Build and package manifests record binary hashes and distinguish native source from companion source.

## Completed checks

On 2026-09-26, all 71 companion regression tests passed. They cover registry collisions, routing translation, overrides, callbacks, selection persistence, active-turn switching, streaming assembly, errors, authentication isolation, cancellation, handoff integrity, and command-aware CLI resume.

The three acceptance scripts passed against the installed native CLI `0.158.0-alpha.2.1`:

- `native-acceptance.py`: actual patch editing, Responses and Chat streaming, reasoning continuity, namespace collisions, provider-table overrides, incomplete-stream failure, interruption, fresh-profile account behavior, restart and null-valued resume.
- `cli-acceptance.py`: real console execution, same-task provider switching with identical upstream model names, saved-ID restart/resume, and directory-filtered `--last`.
- `advanced-acceptance.py`: real stdio MCP discovery and tool execution, native automatic compaction after a 215000-word input, subsequent marker recall, and encrypted-checkpoint handoff conversion with unchanged source history.

The package validation gate repeats these acceptance checks against the bundled baseline CLI and embedded runtime. It also verifies launcher argument handling, model listing, integrity tampering, path traversal rejection, and desktop fingerprint changes. Its successful completion is required before producing the ZIP.

## External checks still pending

The existing private DeepSeek service returned HTTP 503 on 2026-09-26: "DeepSeek V4.1 is loading or undergoing isolated testing. Please retry." Current live inference and desktop inference therefore remain unverified. The example preserves the previously verified model identifier and limits; it contains no private endpoint or credentials.

Installed desktop version: `26.924.2738.0`. Automated checks exercise its native app-server protocol and fingerprint compatibility. Native desktop UI automation is unavailable in this session. Visual picker behavior, concurrent normal/custom windows, GUI restart, and a live DeepSeek GUI turn require the documented manual smoke checks. Protocol acceptance does not establish those visual results.

No normal desktop profile authentication or databases are imported. No remote DeepSeek service changes are made.
