# Local-model harness regression checks

The November 8, 2026 update uses code mode only, an unambiguous JSON envelope for
provider-facing custom tools, bounded tool-contract correction, capability
preflight, task-local HTTP requests, and generation-stall recovery after output
has begun. Initial prefill has no new deadline. See [the harness description](README.md#tool-harness).

The fixes were deployed to the existing Windows backend. Thirteen installed
file hashes were unchanged while testing both saved model deployments.

| Configuration | Padded baseline | Restored asymmetric deployment |
|---|---|---|
| Attention heads across three ranks | 32/32/32 padded | 24/24/16 native |
| Speculative decoding | DSpark block 5 | DSpark block 5 |
| Maximum running requests | 1 | 8 |
| Maximum decode graph batch size | 4 | 8 |
| Matched task scenarios | 8/8 passed | 8/8 passed |
| Malformed tool calls | 0 | 0 |
| No-op shell loops | 0 | 0 |
| Automatic recovery continuations | 0 | 0 |

Both deployments used the Keys-modified DeepSeek V4.1 Flash 1017 weights.
The restored asymmetric ranks mounted the Keys model read-only. The 52 patched
attention output projection (`wo_b`) weight/scale tensors in layers 10–35 matched
Keys revision `de412d2249632903dace23399526e00a145aada5` byte-for-byte.

| Matched scenario | Padded seconds | Asymmetric seconds |
|---|---:|---:|
| Public firmware and stable-release comparison | 14.44 | 16.06 |
| One-time setup, self-signed TLS and authenticated firmware | 88.93 | 51.58 |
| Request deadline and read-only fallback | 24.90 | 24.06 |
| Disconnected selected browser: truthful blocker | 8.76 | 11.42 |
| Existing login without repeating setup | 53.78 | 48.34 |
| Connected browser fixture and vendor feed | 20.05 | 20.37 |
| Installed signed API: firmware comparison | 17.64 | 15.52 |
| Installed signed API: selected browser blocker | 10.46 | 8.33 |

The first six scenarios use the real model and native app-server with automatic
tool selection against disposable loopback HTTP/browser fixtures. The last two
run through the deployed signed task API. No real appliance firmware/settings
were changed. The actual selected browser remained disconnected; the connected
browser scenario used a fixture. Existing history and the paused chat were
preserved. The padded containers, filesystem snapshots, boot files, and hashed
state/cache backups remain available locally for rollback.

The asymmetric rerun also passed all 83 focused Python tests and 11 live API
checks. The model's built-in structured-output, tool-round-trip and vision
readiness checks passed. Runtime inspection confirmed 24/24/16 tensor geometry,
the native sparse-attention kernel, and loaded target/draft weights.

These are saved-deployment quality checks rather than an attention-only speed
or numerical-equivalence study: request/graph settings differ and timings are
single samples with differing generations/cache states. API schema discovery
still involves exploratory requests; the existing-login scenario made four
login POST attempts on asymmetric and three on padded before succeeding. These
API-level rejections are distinct from malformed harness calls.

To rerun the isolated model scenarios with the service dependencies installed:

```powershell
python tools/permissive/tests/harness_device_acceptance.py `
  --native C:/Apps/CodexPermissive/native/codex-app-server.exe `
  --output C:/codex-build/harness-device-evaluation
```

The output directory retains the isolated native profile, complete events,
per-scenario results and actual token usage. Do not commit local profiles,
credentials, full runtime logs or application backups. The repository ignores
the local `out` results and attachment staging directory.
