# Verification record

## Source and runtime

- Native baseline: `rust-v0.157.1`, `36650394c5b38c2990ccf2a3457165ca3e9d9726`.
- Native Rust source, bundled prompts, and checkout lockfile are unchanged. The isolated release build normalizes only 155 local workspace package versions; external dependency identities and checksums remain unchanged.
- Windows build: Rust 1.95.0, x86_64 MSVC, upstream release flags and checksum-pinned V8 artifacts.
- Portable Python: official embeddable 3.13.7. Build and package manifests record binary hashes and distinguish native source from companion source.

## Completed checks

The Windows release build completed successfully and all seven native binaries were produced. The built console reports `codex-cli 0.157.1`. Hidden launcher initialization/model listing passed against this baseline with Python 3.13.7 after fixing Windows stdin inheritance. Launcher fixtures additionally cover Unicode byte streams, EOF, and child exit while caller stdin remains open.

On 2026-09-26, all 71 companion regression tests passed. They cover registry collisions, routing translation, overrides, callbacks, selection persistence, active-turn switching, streaming assembly, errors, authentication isolation, cancellation, handoff integrity, and command-aware CLI resume.

The three acceptance scripts passed against the installed native CLI `0.158.0-alpha.2.1`:

- `native-acceptance.py`: actual patch editing, Responses and Chat streaming, reasoning continuity, namespace collisions, provider-table overrides, incomplete-stream failure, interruption, fresh-profile account behavior, restart and null-valued resume.
- `cli-acceptance.py`: real console execution, same-task provider switching with identical upstream model names, saved-ID restart/resume, and directory-filtered `--last`.
- `advanced-acceptance.py`: real stdio MCP discovery and tool execution, native automatic compaction after a 215000-word input, subsequent marker recall, and encrypted-checkpoint handoff conversion with unchanged source history.

The package validation gate repeats these acceptance checks against the bundled baseline CLI and embedded runtime. It also verifies launcher argument handling, model listing, integrity tampering, path traversal rejection, and desktop fingerprint changes. Its successful completion is required before producing the ZIP.

## External checks still pending

The existing private DeepSeek service returned HTTP 503 on 2026-09-26: "DeepSeek V4.1 is loading or undergoing isolated testing. Please retry." A live smoke through the built 0.157.1 console returned exit 1 and preserved this provider error. Current successful live inference and desktop inference therefore remain unverified. The example preserves the previously verified model identifier and limits; it contains no private endpoint or credentials.

Installed desktop version: `26.924.2738.0`. Its manifest names `app/ChatGPT.exe` as the desktop entry point; `app/Codex.exe` is a command stub. Launch and fingerprint discovery now use the manifest entry point, with containment, package identity, and executable hash checks. A direct startup smoke verified a separate custom desktop process, packaged shim/native child chain, and separate Chromium data while the normal desktop main process remained running. Automated checks exercise its native app-server protocol and fingerprint compatibility. Native desktop UI automation is unavailable in this session. Visual picker behavior, concurrent normal/custom windows, GUI restart, and a live DeepSeek GUI turn require the documented manual smoke checks. Protocol acceptance does not establish those visual results.

No normal desktop profile authentication or databases are imported. No remote DeepSeek service changes are made.


## 2026-09-27 launch follow-up

DeepSeek returned HTTP 200. Live text inference and saved-task resume both exited 0 and recalled the expected token. A live file/tool exercise was cancelled after native execution-policy rejections; live editing remains unverified.

An inherited PowerShell module path prevented the desktop wrapper from finding `Get-FileHash`. The desktop wrapper now removes only its child's `PSModulePath`; a regression with an invalid inherited module path passed. The machine's desktop shortcuts additionally use a process-local module-isolation wrapper around the validated R3 bundle, and actual launch exited 0 with an intentionally invalid parent module path.

The R4 package candidate is not released: its native acceptance check twice exceeded the shutdown deadline after stdin closed. Its desktop launcher check and 71 regression tests passed. The source change does not modify native shutdown or transport behavior. Candidate diagnostics are retained locally in `out/package-validation-r4.log` and `out/package-validation-r4-retry.log`. The working desktop shortcuts continue to use R3.

## Desktop picker correction (2026-09-27)

The installed desktop applies its official-model allowlist when `config/read` does not report `model_catalog_json`, even when `model/list` returns visible custom entries. The companion now reports its generated catalog when no user catalog is configured. Native configuration fields and existing user catalog selections are preserved.

An independent shutdown problem was reproduced: a helper inherited stdout after the native app-server exited, leaving its reader waiting indefinitely. Cleanup now bounds the reader wait after process exit. The regression covers an exited process with an open output queue.

R6 passed 73 regressions, Windows launcher/module isolation checks, native configuration/model discovery, native Responses and Chat acceptance, patch editing, callbacks, failures, cancellation, restart/resume, CLI provider switching, MCP discovery, automatic compaction/recall, and encrypted handoffs. The machine launcher now targets R6. The custom desktop restarted successfully (PID 330468) with its R6 shim; the normal desktop (PID 296568) remained running. Visual picker confirmation remains with the user.

R6 ZIP SHA256: `e47143909e1a918d9d4015e8614bee9b12d5e258fbcece09089ffbfc1f12dc7a`. R5 is an instrumented diagnostic candidate and must not be distributed.

## Visible desktop launch correction (2026-09-27)

The desktop launcher incorrectly requested `WindowStyle Hidden` for the interactive app. A visible-window launch of the validated R6 runtime succeeded, and the user explicitly confirmed the window is visible. Both desktop shortcuts retain their module-isolated entrypoint; the private profile script now performs the R6 hash and desktop fingerprint checks before launching with `WindowStyle Normal`. Source launch-desktop.ps1 uses the same visible-window setting.

R7 full validation reproduced an intermittent shutdown timeout despite identical R6 transport code. It remains an unreleased candidate. The installed runtime stays R6; only the private launcher window setting changed.

## Desktop update recovery (2026-10-03)

OpenAI Codex desktop updated from 26.924.2738.0 to 26.930.3930.0. The R6 desktop fingerprint correctly blocked launch. Revalidation initially hit an intermittent app-server shutdown hang because a descendant inherited the Windows shim's stdout/stderr pipes after Python exited. The shim now bounds its post-exit output-pump joins; the regression spawns a pipe-inheriting helper to exercise this case.

R9 passed 73 companion tests, launcher regression, native Responses and Chat acceptance, actual patch, callbacks, cancellation, restart/resume, CLI, MCP discovery, automatic compaction/recall, and encrypted handoffs. It validated desktop 26.930.3930.0 and relaunched the isolated profile with the R9 shim. Normal desktop remained running. R9 ZIP SHA256: `25de3d6687d78743d79c2be51d88fad434bf86ead0d1d332942dd2dd7d3437`.
