fn main() {
    if std::env::var_os("CARGO_FEATURE_E2E").is_some()
        && std::env::var("PROFILE").as_deref() == Ok("release")
    {
        panic!("UI test hooks are forbidden in release builds");
    }
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "desktop_call",
            "desktop_watch",
            "desktop_unwatch",
        ]),
    ))
    .expect("desktop configuration must be valid");
}
