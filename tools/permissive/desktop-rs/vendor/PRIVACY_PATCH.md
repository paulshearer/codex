The vendored `wry` 0.57.0 is the crates.io release used by Tauri 2.12.3. Its upstream licenses are retained in `wry/LICENSE-APACHE` and `wry/LICENSE-MIT`.

The source changes are confined to `wry/src/webview2/mod.rs`: set `CoreWebView2EnvironmentOptions::is_custom_crash_reporting_enabled` to `true` before creating the environment, and set `ICoreWebView2Settings8::IsReputationCheckingRequired` to `false` before navigating. Microsoft's documented semantics prevent Windows from sending WebView crash data to Microsoft's endpoint and disable SmartScreen reputation uploads for this isolated WebView2 profile. No custom crash uploader is registered. The desktop loads only bundled assets and blocks external navigation; Chrome/Edge browser automation keeps its own security settings. An unsupported runtime fails initialization instead of skipping the reputation setting.

Reference: https://learn.microsoft.com/en-us/microsoft-edge/webview2/reference/win32/icorewebview2environmentoptions3

Reference: https://learn.microsoft.com/en-us/microsoft-edge/webview2/reference/win32/icorewebview2settings8

Temporary credential input also requires `ICoreWebView2Settings4::IsPasswordAutosaveEnabled`
to be `false` before navigation. The app builder disables general form autofill.
These settings prevent new password saving and general form-data saving in the
renderer profile; separately controlled website browsers retain their own profile
settings. Unsupported WebView2 runtimes fail initialization.

Reference: https://learn.microsoft.com/en-us/microsoft-edge/webview2/reference/win32/icorewebview2settings4

Keep this patch when updating Wry. The desktop build checks that Cargo resolved Wry to this local source, so an incompatible Tauri update cannot silently discard it. Local runtime crash files remain subject to Windows/WebView2 behavior; they are not uploaded by the application.

WebView2 remains a Windows component with required runtime diagnostics controlled by Windows. This patch cannot certify zero operating-system telemetry. Reference: https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/data-privacy
