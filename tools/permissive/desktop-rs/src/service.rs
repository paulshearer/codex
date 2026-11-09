use crate::auth::Signer;
use reqwest::{Client, Method, Response};
use serde::Deserialize;
use serde_json::Value;
use std::{
    path::PathBuf,
    process::{Command, Stdio},
    time::Duration,
};
use url::Url;

#[derive(Clone)]
pub struct Config {
    pub root: PathBuf,
    pub home: PathBuf,
    pub python: PathBuf,
    pub deployment: Option<PathBuf>,
    pub expected_vm: Option<String>,
    pub existing_service_only: bool,
    pub base: Url,
    pub source_path: std::ffi::OsString,
}

pub fn validate_transport(target: &str) -> Result<Url, String> {
    let url = Url::parse(target).map_err(|_| "Invalid API connection URL")?;
    let loopback = is_loopback(&url);
    if !matches!(url.scheme(), "http" | "https")
        || url.host().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
        || url.query().is_some()
        || url.path() != "/"
        || (url.scheme() == "http" && !loopback)
    {
        return Err(
            "API connections require TLS or a loopback origin, without URL credentials".into(),
        );
    }
    Ok(url)
}

fn is_loopback(url: &Url) -> bool {
    match url.host() {
        Some(url::Host::Domain("localhost")) => true,
        Some(url::Host::Ipv4(ip)) => ip.is_loopback(),
        Some(url::Host::Ipv6(ip)) => ip.is_loopback(),
        _ => false,
    }
}

const EXISTING_SERVICE_ORIGIN: &str = "http://127.0.0.1:8789";
const EXISTING_SERVICE_UNAVAILABLE: &str =
    "Cannot connect to your existing Windows service. Start that service and reopen the client.";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ClientConnection {
    mode: String,
    origin: String,
}

fn validate_existing_service_connection(bytes: &[u8], base: &Url) -> Result<(), String> {
    let connection: ClientConnection = serde_json::from_slice(bytes)
        .map_err(|_| "Invalid existing-service client connection")?;
    if connection.mode != "existing-service"
        || connection.origin != EXISTING_SERVICE_ORIGIN
        || base.as_str() != format!("{EXISTING_SERVICE_ORIGIN}/")
    {
        return Err("The existing-service client connection must use the local Windows service at port 8789".into());
    }
    Ok(())
}

fn validate_health(
    health: &Value,
    existing_service_only: bool,
    expected_vm: Option<&str>,
    allow_unbound_test_service: bool,
) -> Result<(), String> {
    if health["ok"] != true {
        return Err("The configured Windows service is not ready".into());
    }
    if existing_service_only {
        if health.get("executionRole").is_some() || expected_vm.is_some() {
            return Err("The running service does not match this existing-service client connection".into());
        }
    } else if !allow_unbound_test_service && health["executionRole"] != "controller" {
        return Err("Another service is using this API port. Stop the previous host-execution service before opening this controller".into());
    }
    if let Some(expected) = expected_vm {
        if health["vmId"].as_str() != Some(expected) {
            return Err("The running controller belongs to another VM deployment".into());
        }
    }
    Ok(())
}

impl Config {
    pub fn discover() -> Result<Self, String> {
        let root = std::env::var_os("PERMISSIVE_PACKAGE")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                if cfg!(debug_assertions) {
                    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                        .parent()
                        .unwrap()
                        .to_owned()
                } else {
                    std::env::current_exe()
                        .ok()
                        .and_then(|path| path.parent().map(ToOwned::to_owned))
                        .unwrap_or_default()
                }
            });
        let mut base = validate_transport(
            &std::env::var("PERMISSIVE_API_URL").unwrap_or_else(|_| "http://127.0.0.1:8789".into()),
        )?;
        let deployment_path = std::env::var_os("PERMISSIVE_DEPLOYMENT")
            .map(PathBuf::from)
            .unwrap_or_else(|| root.join("deployment.json"));
        let connection_path = root.join("client-connection.json");
        let mut existing_service_only = false;
        let binding: Option<Value> = if deployment_path.is_file() {
            let value: Value = serde_json::from_slice(&std::fs::read(&deployment_path)
                .map_err(|_| "Cannot read the controller deployment")?)
                .map_err(|_| "Invalid controller deployment")?;
            if value["role"] != "controller" || value["vmId"].as_str().is_none() {
                return Err("The desktop requires an explicit Hyper-V controller deployment".into());
            }
            Some(value)
        } else if connection_path.is_file() {
            let bytes = std::fs::read(&connection_path)
                .map_err(|_| "Cannot read the existing-service client connection")?;
            validate_existing_service_connection(&bytes, &base)?;
            existing_service_only = true;
            None
        } else if is_loopback(&base) && !cfg!(feature = "e2e") {
            return Err("Configure the Hyper-V controller deployment before starting the application".into());
        } else { None };
        if std::env::var_os("PERMISSIVE_API_URL").is_none() {
            if let Some(port) = binding.as_ref().and_then(|v| v["controllerPort"].as_u64()) {
                if !(1024..=65535).contains(&port) { return Err("Invalid bound controller API port".into()); }
                base.set_port(Some(port as u16)).map_err(|_| "Invalid bound controller API origin")?;
            }
        }
        let home = std::env::var_os("PERMISSIVE_HOME")
            .map(PathBuf::from)
            .or_else(|| binding.as_ref().and_then(|v| v["controllerHome"].as_str()).map(PathBuf::from))
            .or_else(|| {
                std::env::var_os("LOCALAPPDATA")
                    .map(|path| PathBuf::from(path).join("CodexPermissive"))
            })
            .ok_or("Cannot locate application data")?;
        let home = std::path::absolute(home).map_err(|_| "Invalid application data directory")?;
        let python = std::env::var_os("PERMISSIVE_PYTHON")
            .map(PathBuf::from)
            .unwrap_or_else(|| root.join("python/python.exe"));
        let source_path = std::env::var_os("PERMISSIVE_SOURCE_PATH")
            .unwrap_or_else(|| root.join("lib").into_os_string());
        let expected_vm = binding.as_ref().and_then(|v| v["vmId"].as_str()).map(ToOwned::to_owned);
        let deployment = binding.map(|_| deployment_path);
        if let Some(path) = &deployment {
            let bound: Value = serde_json::from_slice(&std::fs::read(path).map_err(|_| "Cannot read the controller deployment")?)
                .map_err(|_| "Invalid controller deployment")?;
            if let Some(directory) = bound["controllerHome"].as_str() {
                let bound_home = std::path::absolute(PathBuf::from(directory)).map_err(|_| "Invalid bound controller home")?;
                if home != bound_home {
                    return Err("PERMISSIVE_HOME differs from this VM's bound controller history directory".into());
                }
            }
        }
        Ok(Self {
            root,
            home,
            python,
            deployment,
            expected_vm,
            existing_service_only,
            base,
            source_path,
        })
    }

    async fn controller_ready(&self, api: &Api) -> Result<bool, String> {
        let response = match api.request("GET", "/v1/health", None, Some(Duration::from_secs(3))).await {
            Ok(value) => value,
            Err(_) => return Ok(false),
        };
        let health: Value = response.json().await.map_err(|_| "Invalid controller health response")?;
        validate_health(&health, self.existing_service_only, self.expected_vm.as_deref(),
            cfg!(feature = "e2e") && self.deployment.is_none())?;
        Ok(true)
    }

    fn python_command(&self) -> Command {
        let mut command = Command::new(&self.python);
        command
            .args(["-B", "-X", "utf8"])
            .env("PYTHONPATH", &self.source_path)
            .env("PLAYWRIGHT_BROWSERS_PATH", self.root.join("chromium"));
        let mut paths = vec![
            self.root.join("native"),
            self.root.join("node"),
            self.root.join("python"),
            self.root.join("powershell"),
        ];
        if let Some(current) = std::env::var_os("PATH") {
            paths.extend(std::env::split_paths(&current));
        }
        if let Ok(path) = std::env::join_paths(paths) {
            command.env("PATH", path);
        }
        for (key, _) in std::env::vars_os() {
            let name = key.to_string_lossy().to_uppercase();
            if name.starts_with("OTEL_")
                || name.starts_with("CODEX_OTEL_")
                || name.starts_with("CODEX_TRACE_")
            {
                command.env_remove(key);
            }
        }
        command
            .env("RUST_LOG", "off")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x08000000);
        }
        command
    }

    pub async fn connect(&self) -> Result<Api, String> {
        let config = self.clone();
        tokio::task::spawn_blocking(move || {
            if config.existing_service_only {
                // Updating the client must not initialize or change the existing backend.
                return Signer::load(&config.home);
            }
            std::fs::create_dir_all(&config.home).map_err(|_| "Cannot prepare application data")?;
            let status = config
                .python_command()
                .args(["-m", "permissive.auth", "--home"])
                .arg(&config.home)
                .args(["init-local", "--ensure"])
                .status()
                .map_err(|_| "Cannot initialize local API authentication")?;
            if !status.success() {
                return Err("Local API authentication initialization failed".into());
            }
            Signer::load(&config.home)
        })
        .await
        .map_err(|_| "API authentication initialization failed")?
        .and_then(|signer| {
            let client = Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .no_proxy()
                .connect_timeout(Duration::from_secs(10))
                .build()
                .map_err(|_| "Cannot initialize the API transport")?;
            Ok(Api {
                client,
                signer,
                base: self.base.clone(),
            })
        })
        .map(|api| async move {
            if self.controller_ready(&api).await? {
                crate::mobile::ensure(self).await;
                return Ok(api);
            }
            if self.existing_service_only {
                return Err(EXISTING_SERVICE_UNAVAILABLE.into());
            }
            if !is_loopback(&self.base) {
                return Err("Cannot connect to the configured remote API".into());
            }
            let config = self.clone();
            tokio::task::spawn_blocking(move || {
                let deployment = config.deployment.as_ref().ok_or("Configure the Hyper-V controller deployment before starting the application")?;
                let mut command = Command::new("powershell.exe");
                command.args(["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"])
                    .arg(config.root.join("windows/sandbox/Start-Controller.ps1"))
                    .arg("-BindingPath").arg(deployment).arg("-StartTask")
                    .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
                #[cfg(windows)]
                {
                    use std::os::windows::process::CommandExt;
                    command.creation_flags(0x08000000);
                }
                // The service intentionally survives closing the desktop, preserving tasks.
                let status = command.status().map_err(|_| "The registered Hyper-V controller task could not start")?;
                if !status.success() { return Err("The controller task is unavailable. Complete the elevated sandbox setup before starting the desktop".into()); }
                Ok::<_, String>(())
            })
            .await
            .map_err(|_| "The local execution service could not start")??;
            for _ in 0..60 {
                tokio::time::sleep(Duration::from_millis(500)).await;
                if self.controller_ready(&api).await? {
                    crate::mobile::ensure(self).await;
                    return Ok(api);
                }
            }
            Err("The local execution service did not become ready".into())
        })?
        .await
    }
}

pub struct Api {
    pub client: Client,
    signer: Signer,
    base: Url,
}

impl Api {
    pub async fn request(
        &self,
        method: &str,
        route: &str,
        body: Option<Value>,
        timeout: Option<Duration>,
    ) -> Result<Response, String> {
        let bytes = body
            .map(|value| serde_json::to_vec(&value))
            .transpose()
            .map_err(|_| "Cannot encode API request")?
            .unwrap_or_default();
        let headers = self.signer.headers(method, route, &bytes)?;
        let destination = self.base.join(route).map_err(|_| "Invalid API route")?;
        let mut request = self.client.request(
            Method::from_bytes(method.as_bytes()).map_err(|_| "Invalid API method")?,
            destination,
        );
        for (name, value) in headers {
            request = request.header(name, value);
        }
        if !bytes.is_empty() {
            request = request.body(bytes);
        }
        if let Some(timeout) = timeout {
            request = request.timeout(timeout);
        }
        let response = request.send().await.map_err(|_| "API connection failed")?;
        if !response.status().is_success() {
            return Err(format!(
                "API operation failed (HTTP {})",
                response.status().as_u16()
            ));
        }
        Ok(response)
    }

    pub async fn json(
        &self,
        method: &str,
        route: &str,
        body: Option<Value>,
    ) -> Result<Value, String> {
        self.request(method, route, body, Some(Duration::from_secs(300)))
            .await?
            .json()
            .await
            .map_err(|_| "Invalid API response".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn existing_service_marker_requires_exact_local_installation_origin() {
        let marker = br#"{"mode":"existing-service","origin":"http://127.0.0.1:8789"}"#;
        let origin = validate_transport(EXISTING_SERVICE_ORIGIN).unwrap();
        assert!(validate_existing_service_connection(marker, &origin).is_ok());
        for origin in ["http://127.0.0.1:8791", "http://localhost:8789", "https://pc.example:8789"] {
            assert!(validate_existing_service_connection(marker, &validate_transport(origin).unwrap()).is_err());
        }
    }

    #[test]
    fn existing_service_marker_rejects_malformed_and_expanded_capabilities() {
        let origin = validate_transport(EXISTING_SERVICE_ORIGIN).unwrap();
        for marker in [
            json!({"mode":"controller", "origin":EXISTING_SERVICE_ORIGIN}),
            json!({"mode":"existing-service", "origin":"http://127.0.0.1:8789/"}),
            json!({"mode":"existing-service", "origin":"http://127.0.0.1:8789/path"}),
            json!({"mode":"existing-service", "origin":"https://pc.example:8789"}),
            json!({"mode":"existing-service", "origin":EXISTING_SERVICE_ORIGIN, "startCommand":"PRIVATE"}),
            json!({"mode":"existing-service"}),
        ] {
            let error = validate_existing_service_connection(&serde_json::to_vec(&marker).unwrap(), &origin).unwrap_err();
            assert!(!error.contains("PRIVATE"));
        }
        assert!(validate_existing_service_connection(b"not-json", &origin).is_err());
    }

    #[test]
    fn legacy_health_requires_explicit_existing_service_and_ready_status() {
        let legacy = json!({"ok":true, "activeTasks":0});
        assert!(validate_health(&legacy, true, None, false).is_ok());
        assert!(validate_health(&legacy, false, None, false).is_err());
        for health in [json!({}), json!({"ok":false}), json!({"ok":"true"}),
            json!({"ok":true,"executionRole":null}),
            json!({"ok":true,"executionRole":"guest"}),
            json!({"ok":true,"executionRole":"controller","vmId":"vm-a"})] {
            assert!(validate_health(&health, true, None, false).is_err());
        }
        assert!(validate_health(&legacy, true, Some("vm-a"), false).is_err());
    }

    #[test]
    fn controller_binding_still_requires_matching_role_and_vm() {
        let controller = json!({"ok":true,"executionRole":"controller","vmId":"vm-a"});
        assert!(validate_health(&controller, false, Some("vm-a"), false).is_ok());
        assert!(validate_health(&controller, false, Some("vm-b"), false).is_err());
        assert!(validate_health(&json!({"ok":true,"executionRole":"guest","vmId":"vm-a"}), false, Some("vm-a"), false).is_err());
        assert!(validate_health(&json!({"ok":false,"executionRole":"controller","vmId":"vm-a"}), false, Some("vm-a"), false).is_err());
    }

    #[test]
    fn transport_rejects_plaintext_remote_and_url_credentials() {
        for target in [
            "http://127.0.0.1:8789",
            "http://[::1]:8789",
            "http://localhost:8789",
            "https://pc.example:9443",
        ] {
            assert!(validate_transport(target).is_ok());
        }
        for target in [
            "http://192.168.1.2:8789",
            "https://user:PRIVATE@pc.example",
            "https://pc.example/#PRIVATE",
            "https://pc.example/?token=PRIVATE",
            "https://pc.example/path",
            "PRIVATE-PROMPT",
        ] {
            let error = validate_transport(target).unwrap_err();
            assert!(!error.contains("PRIVATE"));
        }
    }
}
