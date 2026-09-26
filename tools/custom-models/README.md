# Custom models for Windows Codex

This companion starts the unchanged native Codex `rust-v0.157.1` CLI with configurable models. The desktop launcher starts an independent profile with the companion as its app-server shim. Source baseline: `36650394c5b38c2990ccf2a3457165ca3e9d9726`.

## Portable package

Extract the Windows x64 bundle to a writable directory. Paths containing spaces are supported. The bundle includes the native CLI and helpers, Python 3.13.7, the routing companion, two launchers, tests, and a SHA-256 integrity manifest. Windows 11 and the installed official Codex desktop app are required for desktop use.

1. Copy `registry.example.json` to `registry.json`, then set your provider endpoints and model metadata.
2. Open PowerShell in the extracted directory.
3. List configured models: `./codex-custom.exe --list-custom-models`.
4. Start the CLI: `./codex-custom.exe --custom-model local-chat::example`.
5. Run a command: `./codex-custom.exe --custom-model local-chat::example exec "Summarize this project"`.
6. Start the independent desktop profile: `./Codex-Custom-Desktop.exe` or `./windows/launch-desktop.ps1`.

The console launcher uses `<bundle>/.codex-custom` by default. Override it with `--home PATH` or `CUSTOM_CODEX_HOME`. The desktop profile defaults to `%LOCALAPPDATA%/CodexCustomModels`, with separate native home, Electron data, and Chromium data. It does not import the normal profile's authentication or databases. `CODEX_CLI_PATH` is set only for the launched process. The normal desktop app can remain open.

Select aliases such as `provider::model` in the custom desktop picker. The shim translates them to the provider's actual model identifiers and translates returned metadata back. Duplicate upstream model names on different providers remain distinct. Switching models during an active turn is rejected; stop or finish the turn first.

CLI selections persist in the custom home's `custom-models/cli-selection.json`. Desktop selections persist per thread in `custom-models/selections.json`. Resume a particular CLI task with `resume THREAD_ID` or `exec resume THREAD_ID "Continue"`. The companion reads current metadata from the isolated profile, including provider switches. `--last` filters by current provider and working directory, then resolves an explicit task ID before launch. TUI resume excludes noninteractive tasks unless `--include-non-interactive` is supplied; `--all` drops the directory filter. With multiple configured providers, bare interactive `resume` requires an explicit `--custom-model`. Explicit `--custom-model` takes precedence. Every launch creates new loopback URLs and tokens. Resume routes through the current URLs rather than old saved transport addresses.

## Registry

The JSON registry has `version`, `default_model`, `providers`, and `models`. `default_model` names an alias. Provider IDs and per-provider model IDs use letters, digits, dots, underscores, and hyphens; `::` is reserved for aliases. Native built-in provider IDs are reserved. Unknown fields and duplicate JSON keys are rejected.

Provider fields:

| Field | Meaning |
| --- | --- |
| `name`, `base_url` | Display label and endpoint base, normally ending in `/v1` |
| `api_format` | `responses` or `chat_completions` |
| `env_key` | Environment variable containing this provider's bearer token |
| `auth` | Native-style token command: `command`, optional `args`, absolute `cwd`, `timeout_ms`, `refresh_interval_ms` |
| `http_headers` | Explicit provider headers |
| `env_http_headers` | Header names mapped to environment variable names |
| `query_params` | Additional query parameters |
| `stream_idle_timeout_ms` | Native SSE idle deadline; default 600000 |
| `upstream_timeout_seconds` | Upstream socket deadline; default 660 |

Use either `env_key` or command `auth`. Command stdout must contain the bearer token; stderr and credentials are never copied to client errors. A zero refresh interval keeps the cached token until an authentication failure or restart. A 401 clears that cache and is returned unchanged; the next explicit request refreshes the token. Missing optional `env_http_headers` are omitted. Authentication runs inside the facade and is attached only to that provider's requests. The native CLI receives a transient loopback bearer token. Provider credentials and endpoints belong in an uncommitted local registry or environment.

Model fields are `id`, `provider`, `upstream_model`, `display_name`, `context_window`, `max_output_tokens`, `auto_compact_token_limit`, `reasoning_efforts`, `default_reasoning_effort`, `supports_images`, and `supports_tool_search`. Context limits must reserve output space. The context and compaction settings are supplied to native Codex; the facade enforces the output cap upstream. A generated native catalog uses the upstream bundled prompt unchanged. Set `supports_tool_search` when the provider supports client tool discovery; Chat can translate it to function calls. Its default is false, which leaves MCP tools available directly.

`registry.deepseek.example.json` seeds the previously verified DeepSeek model ID, 262144-token context, 196608 compaction threshold, 16384 output cap, medium reasoning, and 600/660-second transport deadlines. Replace its local placeholder with your own service endpoint. The previous integration's private endpoint is not part of the source or portable bundle.

## Transports and tools

Responses providers use the native Responses protocol through a loopback proxy. The proxy resolves authentication, flattens namespace tools where needed, restores names, and reports provider errors. Chat Completions providers use a local Responses compatibility facade. It handles text, reasoning continuity, streamed function calls, namespaced function/MCP tools, and Codex's freeform `apply_patch` through a JSON function with an `input` string. Tools are executed by native Codex and retain its normal permissions and approval behavior.

Chat Completions does not accept image, audio, or other unsupported content. Hosted web search is disabled for Chat models. Unsupported tools, malformed calls, incomplete streams, and provider failures fail the request explicitly. The facade does not report a truncated completion as success. Native automatic retries are disabled to avoid replaying partially completed tool streams.

Encrypted compaction checkpoints cannot be decrypted by a different provider. If a checkpoint has a unique saved readable source in this custom profile, the companion reconstructs a bounded handoff from it. Large sources are summarized by the selected provider; cached handoffs retain source-prefix and summary hashes. It never rewrites the original rollout. Preparation can be run explicitly:

```powershell
./codex-custom.exe --custom-model deepseek::flash --prepare-handoff THREAD_ID
```

Missing, ambiguous, changed, or corrupt sources fail clearly. Keep the custom profile's `sessions` and `handoffs` together when recovering it. Historical images can pass to Responses providers with image capability; Chat Completions rejects them.

## Updates and recovery

The desktop launcher checks bundle integrity and the installed desktop app's version and hashes. After an app update, run `./windows/revalidate.ps1`; it runs companion regression tests, launcher checks, and native acceptance for model listing, streams, actual patch editing, callbacks, cancellation, restart/resume, MCP discovery, long-context compaction, and encrypted-checkpoint handoffs before recording a new desktop fingerprint. Check a new conversation, picker selection, restart/resume, and tool editing in the isolated desktop profile before adopting an updated app build.

If a turn fails, retain its provider error and custom profile for diagnosis. Restart the custom launcher to refresh loopback addresses. If a saved handoff's source changed, move its corresponding JSON cache aside and prepare it again from the retained rollout. A corrupt registry or missing credential is repaired in the local registry/environment, then relaunched. Leave the normal desktop profile intact.

## Building and checking source

From the repository root:

```powershell
./tools/custom-models/windows/bootstrap.ps1
./tools/custom-models/windows/build.ps1
python -m unittest discover -s tools/custom-models -p 'test_*.py' -v
python ./tools/custom-models/native-acceptance.py --native C:/codex-build/permissive/x86_64-pc-windows-msvc/release/codex.exe
python ./tools/custom-models/cli-acceptance.py --native C:/codex-build/permissive/x86_64-pc-windows-msvc/release/codex.exe
python ./tools/custom-models/advanced-acceptance.py --native C:/codex-build/permissive/x86_64-pc-windows-msvc/release/codex.exe
./tools/custom-models/windows/package.ps1
```

The build uses Rust 1.95.0, MSVC, upstream Windows release flags, and source-pinned V8 checksums. Release-lock normalization happens only in an isolated build copy because the upstream tag's lock contains pre-release local workspace versions. The checkout's native sources and lock remain unchanged. Required helpers are packaged with the CLI. Build intermediates live outside the checkout; bundle output and local profiles are ignored.

See [the scoped refusal audit](REFUSAL-AUDIT.md) and [verification evidence](VERIFICATION.md) for findings and tested limits.

The companion is organized into three reviewable parts: registry/routing, transport/handoffs, and Windows delivery/acceptance. Native Rust source is unchanged.
