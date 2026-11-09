//! Local-only companion management. No process or URL capability crosses IPC.
use crate::service::Config;
use serde_json::Value;
use std::{io::Read, process::{Command, Stdio}, time::{Duration, Instant}};

pub async fn manage(config: &Config, action: &str, device: Option<&str>) -> Result<Value, String> {
    if !matches!(action, "Status" | "Pair" | "Revoke" | "Disable" | "Ensure") {
        return Err("Unsupported companion operation".into());
    }
    if action == "Revoke" && !device.is_some_and(|id| id.len() == 64 && id.bytes().all(|c| c.is_ascii_hexdigit())) {
        return Err("Invalid enrolled device identity".into());
    }
    let config = config.clone();
    let action = action.to_owned();
    let device = device.map(str::to_owned);
    tokio::task::spawn_blocking(move || {
        let mut command = Command::new("powershell.exe");
        let mobile_source = config.root.join("mobile-lib");
        command.args(["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"])
            .arg(config.root.join("windows/mobile-companion.ps1"))
            .arg("-Action").arg(&action)
            .arg("-PackageDirectory").arg(&config.root)
            .arg("-HomeDirectory").arg(&config.home)
            .arg("-PythonPath").arg(&config.python)
            .arg("-SourcePath").arg(if mobile_source.is_dir() { mobile_source.as_os_str() } else { &config.source_path })
            .arg("-ApiUrl").arg(config.base.as_str())
            .stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null());
        if let Some(device) = device { command.arg("-DeviceId").arg(device); }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        let mut child = command.spawn().map_err(|_| "Cannot run companion setup")?;
        let stdout = child.stdout.take().ok_or("Companion output is unavailable")?;
        let reader = std::thread::spawn(move || {
            let mut bytes = Vec::new();
            stdout.take(1024 * 1024 + 1).read_to_end(&mut bytes).map(|_| bytes)
        });
        let deadline = Instant::now() + Duration::from_secs(if action == "Pair" { 90 } else { 15 });
        let status = loop {
            if let Some(status) = child.try_wait().map_err(|_| "Cannot inspect companion setup")? { break status; }
            if Instant::now() >= deadline {
                // Kill only this helper's process tree; no user/controller tasks.
                #[cfg(windows)]
                {
                    use std::os::windows::process::CommandExt;
                    let _ = Command::new("taskkill.exe").args(["/PID", &child.id().to_string(), "/T", "/F"])
                        .creation_flags(0x08000000).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).status();
                }
                let _ = child.kill(); let _ = child.wait();
                return Err("Companion setup timed out. Check Tailscale and retry.".into());
            }
            std::thread::sleep(Duration::from_millis(50));
        };
        let stdout = reader.join().map_err(|_| "Cannot read companion response")?
            .map_err(|_| "Cannot read companion response")?;
        if !status.success() || stdout.len() > 1024 * 1024 {
            return Err("Companion setup failed. Check Tailscale and the Windows controller, then retry.".into());
        }
        serde_json::from_slice(&stdout).map_err(|_| "Invalid companion setup response".into())
    }).await.map_err(|_| "Companion operation failed")?
}

/// Companion failures must never prevent normal controller/desktop readiness.
pub async fn ensure(config: &Config) {
    let _ = manage(config, "Ensure", None).await;
}
