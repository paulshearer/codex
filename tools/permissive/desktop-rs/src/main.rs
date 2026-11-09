#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod auth;
mod desktop;
mod events;
mod mobile;
mod service;

use desktop::{Shell, desktop_call, desktop_unwatch, desktop_watch};
use tauri::Manager;

fn main() {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::System::Diagnostics::Debug::{
            GetErrorMode, SEM_NOGPFAULTERRORBOX, SetErrorMode,
        };
        // Process-local Windows Error Reporting opt-out, inherited by children.
        SetErrorMode(GetErrorMode() | SEM_NOGPFAULTERRORBOX);
    }
    // Run before creating the runtime or any worker threads. Environment-based
    // debugger/loader overrides must not reopen the production inspection port.
    for key in [
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
        "WEBVIEW2_PIPE_FOR_SCRIPT_DEBUGGER",
        "WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER",
        "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER",
        "WEBVIEW2_USER_DATA_FOLDER",
    ] {
        unsafe {
            std::env::remove_var(key);
        }
    }
    // Libraries and panic diagnostics must never format user content or keys.
    log::set_max_level(log::LevelFilter::Off);
    std::panic::set_hook(Box::new(|_| {}));
    if std::env::args().any(|value| value == "--version") {
        print_identity("Codex Permissive 1.0.0 (Rust desktop; Tauri)");
        return;
    }
    if std::env::args().any(|value| value == "--help") {
        print_identity(
            "Codex Permissive Rust desktop. Launch without arguments to open the app; --start-service starts the local service without a window.",
        );
        return;
    }
    let Ok(config) = service::Config::discover() else {
        std::process::exit(1);
    };
    if std::env::args().any(|value| value == "--start-service") {
        let ready = tauri::async_runtime::block_on(config.connect()).is_ok();
        desktop::record(
            &config,
            if ready {
                "service.ready"
            } else {
                "service.failed"
            },
        );
        std::process::exit(if ready { 0 } else { 1 });
    }
    let result = tauri::Builder::default()
        .manage(Shell::new(config.clone()))
        .invoke_handler(tauri::generate_handler![desktop_call, desktop_watch, desktop_unwatch])
        .setup(move |app| {
            #[allow(unused_mut)]
            let mut builder = tauri::WebviewWindowBuilder::from_config(app.handle(), &app.config().app.windows[0])?
                .data_directory(config.home.join("desktop-webview"))
                .general_autofill_enabled(false)
                .additional_browser_args("--disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --disable-background-networking --disable-crash-reporter --disable-breakpad --disable-sync --no-first-run")
                .on_navigation(is_app_navigation)
                .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny);
            #[cfg(all(debug_assertions, feature = "e2e"))]
            if let Ok(port) = std::env::var("PERMISSIVE_E2E_DEBUG_PORT") {
                let port: u16 = port.parse()?;
                builder = builder.additional_browser_args(&format!("--remote-debugging-port={port} --remote-debugging-address=127.0.0.1 --disable-features=msWebOOUI,msPdfOOUI,msSmartScreenProtection --disable-background-networking --disable-crash-reporter --disable-breakpad --disable-sync --no-first-run"));
            }
            let window = builder.build()?;
            window.on_menu_event(|window, event| desktop::copy_menu(&window.state::<Shell>(), event.id().as_ref()));
            window.on_window_event({ let handle = app.handle().clone(); move |event| { if matches!(event, tauri::WindowEvent::Destroyed) { handle.state::<Shell>().cancel_watch(); } } });
            desktop::record(&config, "desktop.started");
            Ok(())
        })
        .run(tauri::generate_context!());
    if result.is_err() { /* GUI failures are content-free and never uploaded. */ }
}

fn print_identity(text: &'static str) {
    use std::io::Write;
    let _ = writeln!(std::io::stdout(), "{text}");
}

fn is_app_navigation(url: &url::Url) -> bool {
    url.username().is_empty()
        && url.password().is_none()
        && url.port().is_none()
        && if cfg!(windows) {
            url.scheme() == "http" && url.host_str() == Some("tauri.localhost")
        } else {
            url.scheme() == "tauri" && url.host_str() == Some("localhost")
        }
}

#[cfg(test)]
mod navigation_tests {
    #[test]
    fn bundled_origin_cannot_be_replaced_by_an_external_or_local_server() {
        let accepted = if cfg!(windows) {
            "http://tauri.localhost/index.html"
        } else {
            "tauri://localhost/index.html"
        };
        for target in [accepted] {
            assert!(super::is_app_navigation(&url::Url::parse(target).unwrap()));
        }
        for target in [
            "https://external.example/",
            "https://tauri.localhost/index.html",
            "http://tauri.localhost:9443/",
            "https://tauri.localhost.evil.example/",
            "http://user:secret@tauri.localhost/",
            "file:///C:/private.html",
        ] {
            assert!(!super::is_app_navigation(&url::Url::parse(target).unwrap()));
        }
    }
}
