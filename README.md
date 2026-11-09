# Codex Permissive

An independent Windows desktop application, CLI, and authenticated task API. Inference uses the local DeepSeek V4.1 Flash instance on Sparky. New deployments run commands, desktop control, and browsers inside a dedicated Windows Hyper-V VM. The host keeps conversation history and controls the VM perimeter; the official Codex desktop is not required. Existing host installations remain supported through an explicit signed-service connection, as documented in the [release review](tools/permissive/RELEASE_REVIEW.md).

The [native Android companion](android/README.md) uses a Sparky Chat themed interface to select Windows workspaces, share conversations and control tasks over Tailscale. Pair a phone through the Windows **Pair Android** panel; enrolled Android Keystore keys authenticate access.

The native runtime is rebased onto upstream `rust-v0.162.1`, commit `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`. The guest runs the separately compiled Permissive runtime as its local administrator, without harness action reviews. One task controls the shared guest desktop at a time. Host vSwitch rules isolate the guest from the host, LAN, and tailnet, with public Internet access and a fixed inference relay. A missing VM binding fails startup; execution never falls back to the host.

The VM uses a portable VHDX and cold Hyper-V export/import bundle, without BitLocker, Secure Boot, or vTPM dependencies. See [provisioning, isolation, recovery, and moving between hosts](tools/permissive/windows/sandbox/README.md). Elevated Hyper-V provisioning and live guest acceptance are required before deployment can be considered verified.

See [build, installation, browser setup, CLI, and API instructions](tools/permissive/README.md). The application and its OpenAPI specification live in `tools/permissive`; reusable provider compatibility code lives in `tools/custom-models`.

The [harness regression report](tools/permissive/HARNESS_REGRESSION.md) records the deployed tool fixes and matching padded/asymmetric attention tests.

The original upstream README is preserved in [UPSTREAM_README.md](UPSTREAM_README.md). Upstream code retains its Apache 2.0 license and notices.
