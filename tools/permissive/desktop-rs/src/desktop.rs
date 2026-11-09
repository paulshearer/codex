use crate::{
    events::EventDecoder,
    service::{Api, Config},
};
use futures_util::StreamExt;
use serde_json::{Value, json};
use base64::Engine;
use std::{
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{Duration, SystemTime, UNIX_EPOCH},
};
use tauri::{
    Emitter, WebviewWindow,
    menu::{Menu, MenuItem},
};
use tokio::sync::OnceCell;
use tokio_util::sync::CancellationToken;

pub struct Shell {
    pub config: Config,
    client: OnceCell<Result<Arc<Api>, String>>,
    pub watch: Mutex<Option<CancellationToken>>,
    pub menu: Mutex<Option<(Option<String>, String)>>,
}

impl Shell {
    pub fn new(config: Config) -> Self {
        Self {
            config,
            client: OnceCell::new(),
            watch: Mutex::new(None),
            menu: Mutex::new(None),
        }
    }
    async fn api(&self) -> Result<&Arc<Api>, String> {
        self.client
            .get_or_init(|| async {
                let result = self.config.connect().await.map(Arc::new);
                record(
                    &self.config,
                    if result.is_ok() {
                        "service.ready"
                    } else {
                        "service.failed"
                    },
                );
                result
            })
            .await
            .as_ref()
            .map_err(Clone::clone)
    }
    pub fn cancel_watch(&self) {
        if let Ok(mut watch) = self.watch.lock() {
            if let Some(token) = watch.take() {
                token.cancel();
            }
        }
    }
}

// This accepts only literal metadata supplied by the shell, never user arguments.
pub fn record(config: &Config, event: &'static str) {
    use std::io::Write;
    let file = config.home.join("desktop.log");
    if std::fs::metadata(&file).is_ok_and(|meta| meta.len() > 5 * 1024 * 1024) {
        let _ = std::fs::rename(&file, file.with_extension("log.previous"));
    }
    let _ = std::fs::create_dir_all(&config.home);
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(file)
    {
        let seconds = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let _ = writeln!(file, "{seconds} {event}");
    }
}

fn require_main(window: &WebviewWindow) -> Result<(), String> {
    if window.label() == "main" {
        Ok(())
    } else {
        Err("Desktop operation is unavailable for this window".into())
    }
}
fn string(args: &[Value], index: usize) -> Result<&str, String> {
    args.get(index)
        .and_then(Value::as_str)
        .ok_or_else(|| "Missing desktop operation argument".into())
}
fn optional(args: &[Value], index: usize) -> Option<&str> {
    args.get(index)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
}
fn value(args: &[Value], index: usize) -> Result<Value, String> {
    args.get(index)
        .filter(|value| !value.is_null())
        .cloned()
        .ok_or_else(|| "Missing desktop operation argument".into())
}
fn segment(input: &str) -> String {
    url::form_urlencoded::byte_serialize(input.as_bytes()).collect()
}
fn query(values: &[(&str, Option<&str>)]) -> String {
    let mut query = url::form_urlencoded::Serializer::new(String::new());
    for (key, value) in values {
        if let Some(value) = value {
            query.append_pair(key, value);
        }
    }
    let encoded = query.finish();
    if encoded.is_empty() {
        String::new()
    } else {
        format!("?{encoded}")
    }
}

#[cfg(all(debug_assertions, feature = "e2e"))]
fn fixture() -> Value {
    std::env::var_os("PERMISSIVE_E2E_DIALOG_FILE")
        .and_then(|path| std::fs::read(path).ok())
        .and_then(|value| serde_json::from_slice(&value).ok())
        .unwrap_or(Value::Null)
}

async fn open_files(window: &WebviewWindow, skills: bool) -> Vec<PathBuf> {
    #[cfg(all(debug_assertions, feature = "e2e"))]
    {
        if let Some(files) = fixture().get("openFiles").and_then(Value::as_array) {
            return files
                .iter()
                .filter_map(Value::as_str)
                .map(PathBuf::from)
                .collect();
        }
    }
    let dialog = rfd::AsyncFileDialog::new().set_parent(window);
    if skills {
        dialog
            .set_title("Select SKILL.md files")
            .add_filter("Skill documents", &["md"])
            .pick_files()
            .await
            .unwrap_or_default()
            .into_iter()
            .map(|file| file.path().to_owned())
            .collect()
    } else {
        dialog
            .set_title("Import skill bundle")
            .add_filter("Skill bundle", &["zip"])
            .pick_file()
            .await
            .into_iter()
            .map(|file| file.path().to_owned())
            .collect()
    }
}

#[tauri::command]
pub async fn desktop_call(
    window: WebviewWindow,
    shell: tauri::State<'_, Shell>,
    operation: String,
    args: Vec<Value>,
) -> Result<Value, String> {
    require_main(&window)?;
    match operation.as_str() {
        "mobile-status" => return crate::mobile::manage(&shell.config, "Status", None).await,
        "mobile-pair" => { shell.api().await?; return crate::mobile::manage(&shell.config, "Pair", None).await; }
        "mobile-revoke" => return crate::mobile::manage(&shell.config, "Revoke", Some(string(&args, 0)?)).await,
        "mobile-disable" => return crate::mobile::manage(&shell.config, "Disable", None).await,
        _ => {}
    }
    // No arbitrary method, URL, filesystem or process capability is exposed.
    match operation.as_str() {
        "choose-directory" => {
            #[cfg(all(debug_assertions, feature = "e2e"))]
            {
                if let Some(path) = fixture().get("directory") {
                    return Ok(path.clone());
                }
            }
            return Ok(rfd::AsyncFileDialog::new()
                .set_parent(&window)
                .set_title("Workspace directory")
                .pick_folder()
                .await
                .map(|file| json!(file.path()))
                .unwrap_or(Value::Null));
        }
        "choose-skills" => return Ok(json!(open_files(&window, true).await)),
        "save-table" => {
            let bytes: Vec<u8> = serde_json::from_value(value(&args, 0)?.clone())
                .map_err(|_| "Invalid table export")?;
            if bytes.len() > 32 * 1024 * 1024 || !bytes.starts_with(b"PK\x03\x04") {
                return Err("Invalid table workbook".into());
            }
            #[cfg(all(debug_assertions, feature = "e2e"))]
            let file = fixture()
                .get("saveTable")
                .and_then(Value::as_str)
                .map(PathBuf::from);
            #[cfg(not(all(debug_assertions, feature = "e2e")))]
            let file: Option<PathBuf> = None;
            let file = match file {
                Some(file) => Some(file),
                None => rfd::AsyncFileDialog::new()
                    .set_parent(&window)
                    .set_title("Export table as Excel")
                    .set_file_name("table-export.xlsx")
                    .add_filter("Excel workbook", &["xlsx"])
                    .save_file()
                    .await
                    .map(|file| file.path().to_owned()),
            };
            let Some(file) = file else {
                return Ok(Value::Null);
            };
            std::fs::write(&file, bytes).map_err(|_| "Cannot save the table workbook")?;
            return Ok(json!(file));
        }
        "copy-extension-folder" => {
            let directory = shell
                .config
                .root
                .join("browser/extension")
                .to_string_lossy()
                .into_owned();
            arboard::Clipboard::new()
                .and_then(|mut clipboard| clipboard.set_text(&directory))
                .map_err(|_| "Cannot copy the extension directory")?;
            return Ok(json!(directory));
        }
        // These operations may access the service. Unknown operations fail before startup.
        "health"
        | "list"
        | "submit"
        | "get"
        | "cancel"
        | "continue"
        | "pause"
        | "resume"
        | "retry"
        | "task-menu"
        | "workspaces"
        | "save-workspace"
        | "rename-chat"
        | "fork-chat"
        | "delete-from"
        | "edit-message"
        | "delete-message"
        | "regenerate"
        | "skills"
        | "skill-document"
        | "skill-category"
        | "register-skills"
        | "import-skills"
        | "export-skills"
        | "browsers"
        | "credentials"
        | "prepare-credential"
        | "save-credential"
        | "revoke-credential"
        | "connect-browser-profile"
        | "setup-browser-profile"
        | "artifact" => {}
        "chats" | "chat" | "chat-events" | "steer" | "queue-followup" | "remove-followup" | "resume-queue" | "delete-chat" | "delete-workspace" | "attach-files" | "upload-attachment" | "attachment-info" | "save-attachment" | "remove-attachment" => {}
        _ => return Err("Unsupported desktop operation".into()),
    }
    let api = shell.api().await?;
    let (method, route, body) = match operation.as_str() {
        "credentials" => ("GET", format!("/v1/credentials{}", query(&[("workspaceId", Some(string(&args, 0)?))])), None),
        "prepare-credential" => ("POST", "/v1/credentials/prepare".into(), Some(json!({"workspaceId": string(&args, 0)?}))),
        "save-credential" => ("POST", "/v1/credentials".into(), Some(value(&args, 0)?)),
        "revoke-credential" => ("DELETE", format!("/v1/credentials{}{}", optional(&args, 1).map(|id| format!("/{}", segment(id))).unwrap_or_default(), query(&[("workspaceId", Some(string(&args, 0)?))])), None),
        "upload-attachment" => ("POST", "/v1/attachments".into(), Some(value(&args, 0)?)),
        "attachment-info" | "remove-attachment" => (if operation == "attachment-info" { "GET" } else { "DELETE" }, format!("/v1/attachments/{}", segment(string(&args, 0)?)), None),
        "attach-files" => {
            #[cfg(all(debug_assertions, feature = "e2e"))]
            let files = fixture().get("attachments").and_then(Value::as_array).map(|values| values.iter().filter_map(Value::as_str).map(PathBuf::from).collect::<Vec<_>>());
            #[cfg(not(all(debug_assertions, feature = "e2e")))]
            let files: Option<Vec<PathBuf>> = None;
            let files = match files {
                Some(files) => files,
                None => rfd::AsyncFileDialog::new().set_parent(&window).set_title("Attach files to chat").pick_files().await.unwrap_or_default().into_iter().map(|file| file.path().to_owned()).collect(),
            };
            let remaining = args.get(1).and_then(Value::as_u64).unwrap_or(10).min(10) as usize;
            let remaining_bytes = args.get(2).and_then(Value::as_u64).unwrap_or(32 * 1024 * 1024).min(32 * 1024 * 1024);
            if files.len() > remaining { return Err("Attach at most 10 files per message".into()); }
            let mut total = 0;
            for file in &files {
                let size = std::fs::metadata(file).map_err(|_| "Cannot read selected file")?.len();
                if size == 0 || size > 16 * 1024 * 1024 { return Err("Files must be nonempty and at most 16 MiB".into()); }
                total += size;
                if total > remaining_bytes { return Err("Attachments exceed the 32 MiB message limit".into()); }
            }
            let mut uploaded: Vec<Value> = Vec::new();
            for file in files {
                // File bytes are read only from the native picker selection.
                use std::io::Read;
                let mut bytes = Vec::new();
                let result = match std::fs::File::open(&file).and_then(|handle| handle.take(16 * 1024 * 1024 + 1).read_to_end(&mut bytes)) {
                    Ok(_) if !bytes.is_empty() && bytes.len() <= 16 * 1024 * 1024 => api.json("POST", "/v1/attachments", Some(json!({"name": file.file_name().unwrap_or_default().to_string_lossy(), "workspaceId": string(&args, 0)?, "data": base64::engine::general_purpose::STANDARD.encode(bytes)}))).await,
                    _ => Err("Cannot read selected file or file exceeds 16 MiB".into()),
                };
                match result {
                    Ok(value) => uploaded.push(value),
                    Err(error) => {
                        for item in &uploaded { if let Some(id) = item["id"].as_str() { let _ = api.json("DELETE", &format!("/v1/attachments/{}", segment(id)), None).await; } }
                        return Err(error);
                    }
                }
            }
            return Ok(json!(uploaded));
        }
        "save-attachment" => {
            let route = format!("/v1/attachments/{}", segment(string(&args, 0)?));
            let info = api.json("GET", &route, None).await?;
            let Some(file) = rfd::AsyncFileDialog::new().set_parent(&window).set_title("Save attached file").set_file_name(info["name"].as_str().unwrap_or("attachment")).save_file().await else { return Ok(Value::Null); };
            let bytes = api.request("GET", &format!("{route}/content"), None, Some(Duration::from_secs(300))).await?.bytes().await.map_err(|_| "Cannot read attached file")?;
            std::fs::write(file.path(), bytes).map_err(|_| "Cannot save attached file")?;
            return Ok(json!(file.path()));
        }
        "health" => ("GET", "/v1/health".into(), None),
        "delete-chat" | "delete-workspace" => ("POST", format!("/v1/{}/{}/secure-delete", if operation == "delete-chat" { "chats" } else { "workspaces" }, segment(string(&args, 0)?)), Some(json!({"confirm": string(&args, 0)?, "passes": 7}))),
        "fork-chat" | "delete-from" | "delete-message" | "regenerate" | "edit-message" => ("POST", format!("/v1/chats/{}/{}", segment(string(&args, 0)?), if operation == "fork-chat" { "fork" } else { &operation }), Some(value(&args, 1)?)),
        "chats" => ("GET", format!("/v1/chats{}", query(&[("workspaceId", optional(&args, 0)), ("cursor", optional(&args, 1))])), None),
        "chat" => ("GET", format!("/v1/chats/{}{}", segment(string(&args, 0)?), query(&[("segmentId", optional(&args, 1))])), None),
        "chat-events" => ("GET", format!("/v1/chats/{}/events{}", segment(string(&args, 0)?), query(&[("format", Some("json")), ("view", Some("mobile")), ("after", optional(&args, 1)), ("through", optional(&args, 2)), ("segmentId", optional(&args, 3))])), None),
        "steer" => ("POST", format!("/v1/tasks/{}/steer", segment(string(&args, 0)?)), Some(value(&args, 1)?)),
        "queue-followup" => ("POST", format!("/v1/chats/{}/followups", segment(string(&args, 0)?)), Some(value(&args, 1)?)),
        "remove-followup" => ("DELETE", format!("/v1/chats/{}/followups/{}", segment(string(&args, 0)?), segment(string(&args, 1)?)), None),
        "resume-queue" => ("POST", format!("/v1/chats/{}/followups/resume", segment(string(&args, 0)?)), None),
        "list" => (
            "GET",
            format!(
                "/v1/tasks{}",
                query(&[
                    ("workspaceId", optional(&args, 0)),
                    ("cursor", optional(&args, 1))
                ])
            ),
            None,
        ),
        "workspaces" => ("GET", "/v1/workspaces".into(), None),
        "browsers" => ("GET", "/v1/browsers".into(), None),
        "submit" => ("POST", "/v1/tasks".into(), Some(value(&args, 0)?)),
        "get" => (
            "GET",
            format!("/v1/tasks/{}", segment(string(&args, 0)?)),
            None,
        ),
        "cancel" | "pause" | "resume" | "retry" => (
            "POST",
            format!("/v1/tasks/{}/{}", segment(string(&args, 0)?), operation),
            None,
        ),
        "continue" => {
            let mut body = args
                .get(2)
                .and_then(Value::as_object)
                .cloned()
                .unwrap_or_default();
            body.insert("prompt".into(), json!(string(&args, 1)?));
            (
                "POST",
                format!("/v1/tasks/{}/continue", segment(string(&args, 0)?)),
                Some(Value::Object(body)),
            )
        }
        "rename-chat" => (
            "PATCH",
            format!("/v1/chats/{}", segment(string(&args, 0)?)),
            Some(json!({"title": string(&args, 1)?})),
        ),
        "save-workspace" => (
            if optional(&args, 0).is_some() {
                "PATCH"
            } else {
                "POST"
            },
            optional(&args, 0)
                .map(|id| format!("/v1/workspaces/{}", segment(id)))
                .unwrap_or_else(|| "/v1/workspaces".into()),
            Some(value(&args, 1)?),
        ),
        "skills" => (
            "GET",
            format!(
                "/v1/skills{}",
                query(&[
                    ("workspaceId", optional(&args, 0)),
                    ("workingDirectory", optional(&args, 1))
                ])
            ),
            None,
        ),
        "skill-document" => (
            "GET",
            format!(
                "/v1/skills/document{}",
                query(&[("path", Some(string(&args, 0)?))])
            ),
            None,
        ),
        "skill-category" => (
            "PATCH",
            "/v1/skills/categories".into(),
            Some(json!({"paths": value(&args, 0)?, "category": string(&args, 1)?})),
        ),
        "register-skills" => (
            "POST",
            "/v1/skills/register".into(),
            Some(json!({"paths": value(&args, 0)?})),
        ),
        "connect-browser-profile" => (
            "POST",
            "/v1/browsers/connect".into(),
            Some(json!({"browser": string(&args, 0)?, "profileId": string(&args, 1)?})),
        ),
        "setup-browser-profile" => {
            let browser = string(&args, 0)?;
            let profile_id = string(&args, 1)?;
            let catalog = api.json("GET", "/v1/browsers", None).await?;
            let selected = catalog["data"]
                .as_array()
                .and_then(|items| items.iter().find(|item| item["id"] == browser))
                .and_then(|item| item["installedProfiles"].as_array())
                .and_then(|items| items.iter().find(|item| item["id"] == profile_id))
                .ok_or("The selected browser profile is no longer available")?;
            let name = selected["name"].as_str().unwrap_or("selected profile");
            let account = selected["account"]
                .as_str()
                .or_else(|| selected["directory"].as_str())
                .unwrap_or("");
            let text = format!(
                "Enable Codex Permissive in {name}?\n\n{account}\n\nThis enables Developer mode, loads the Codex Permissive extension, and connects this profile so the app can control its tabs. A browser setup window will open and be controlled briefly."
            );
            #[cfg(all(debug_assertions, feature = "e2e"))]
            let confirmation =
                if let Some(answer) = fixture().get("confirm").and_then(Value::as_bool) {
                    answer
                } else {
                    confirm(&window, &text).await
                };
            #[cfg(not(all(debug_assertions, feature = "e2e")))]
            let confirmation = confirm(&window, &text).await;
            if !confirmation {
                return Ok(json!({"cancelled": true}));
            }
            let result = api
                .json(
                    "POST",
                    "/v1/browsers/setup",
                    Some(json!({"browser": browser, "profileId": profile_id, "confirmed": true})),
                )
                .await;
            let _ = window.show();
            let _ = window.set_focus();
            return result;
        }
        "import-skills" => {
            let files = open_files(&window, false).await;
            let Some(file) = files.first() else {
                return Ok(Value::Null);
            };
            (
                "POST",
                "/v1/skills/import".into(),
                Some(json!({"file": file})),
            )
        }
        "export-skills" => {
            #[cfg(all(debug_assertions, feature = "e2e"))]
            let file = if let Some(file) = fixture().get("saveFile").and_then(Value::as_str) {
                Some(PathBuf::from(file))
            } else {
                save_file(&window).await
            };
            #[cfg(not(all(debug_assertions, feature = "e2e")))]
            let file = save_file(&window).await;
            let Some(file) = file else {
                return Ok(Value::Null);
            };
            let bytes = api
                .request(
                    "POST",
                    "/v1/skills/export",
                    Some(json!({"paths": value(&args, 0)?})),
                    Some(Duration::from_secs(300)),
                )
                .await?
                .bytes()
                .await
                .map_err(|_| "Cannot read skill export")?;
            std::fs::write(&file, bytes).map_err(|_| "Cannot save the skill bundle")?;
            return Ok(json!(file));
        }
        "artifact" => {
            let id = string(&args, 0)?;
            let artifact = string(&args, 1)?;
            for component in [id, artifact] {
                if component.is_empty()
                    || component.contains("..")
                    || !component
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || b"_.-".contains(&byte))
                {
                    return Err("Invalid artifact identifier".into());
                }
            }
            let bytes = api
                .request(
                    "GET",
                    &format!("/v1/tasks/{id}/artifacts/{artifact}"),
                    None,
                    Some(Duration::from_secs(300)),
                )
                .await?
                .bytes()
                .await
                .map_err(|_| "Cannot read the task artifact")?;
            let Some(destination) = rfd::AsyncFileDialog::new()
                .set_parent(&window).set_title("Download task artifact")
                .set_file_name(artifact).save_file().await else { return Ok(Value::Null); };
            let file = destination.path();
            std::fs::write(file, bytes).map_err(|_| "Cannot save the task artifact")?;
            // Guest output is data at the host perimeter. Never dispatch it to
            // its OS file association (which could execute active content).
            return Ok(json!({"downloaded": true, "path": file}));
        }
        "task-menu" => {
            let task = api
                .json(
                    "GET",
                    &format!("/v1/tasks/{}", segment(string(&args, 0)?)),
                    None,
                )
                .await?;
            let id = task["id"]
                .as_str()
                .ok_or("Invalid task response")?
                .to_owned();
            let thread = task["threadId"].as_str().map(str::to_owned);
            *shell.menu.lock().map_err(|_| "Cannot prepare task menu")? =
                Some((thread.clone(), id.clone()));
            let label = thread
                .as_ref()
                .map(|id| format!("Chat ID: {id}"))
                .unwrap_or_else(|| "Chat ID available when the task starts".into());
            let title = MenuItem::new(&window, label, false, None::<&str>)
                .map_err(|_| "Cannot prepare task menu")?;
            let chat = MenuItem::with_id(
                &window,
                "copy-chat",
                "Copy chat ID",
                thread.is_some(),
                None::<&str>,
            )
            .map_err(|_| "Cannot prepare task menu")?;
            let task_id =
                MenuItem::with_id(&window, "copy-task", "Copy task ID", true, None::<&str>)
                    .map_err(|_| "Cannot prepare task menu")?;
            let menu = Menu::with_items(&window, &[&title, &chat, &task_id])
                .map_err(|_| "Cannot prepare task menu")?;
            #[cfg(all(debug_assertions, feature = "e2e"))]
            if let Some(action) = fixture().get("menuAction").and_then(Value::as_str) {
                copy_menu(&shell, action);
                return Ok(json!({"chatId": thread, "taskId": id}));
            }
            window
                .popup_menu(&menu)
                .map_err(|_| "Cannot show task menu")?;
            return Ok(json!({"chatId": thread, "taskId": id}));
        }
        _ => return Err("Unsupported desktop operation".into()),
    };
    api.json(method, &route, body).await
}

async fn confirm(window: &WebviewWindow, text: &str) -> bool {
    #[cfg(windows)]
    {
        use windows_sys::Win32::UI::WindowsAndMessaging::{
            IDOK, MB_DEFBUTTON2, MB_ICONQUESTION, MB_OKCANCEL, MessageBoxW,
        };
        let Ok(hwnd) = window.hwnd() else {
            return false;
        };
        let hwnd = hwnd.0 as usize;
        let message: Vec<u16> = text.encode_utf16().chain(std::iter::once(0)).collect();
        let title: Vec<u16> = "Enable browser connection?"
            .encode_utf16()
            .chain(std::iter::once(0))
            .collect();
        // Preserve the previous desktop's Cancel default. The owner window is
        // disabled by MessageBox while this explicit confirmation is displayed.
        return tokio::task::spawn_blocking(move || unsafe {
            MessageBoxW(
                hwnd as _,
                message.as_ptr(),
                title.as_ptr(),
                MB_OKCANCEL | MB_DEFBUTTON2 | MB_ICONQUESTION,
            ) == IDOK
        })
        .await
        .unwrap_or(false);
    }
    #[cfg(not(windows))]
    {
        rfd::AsyncMessageDialog::new()
            .set_parent(window)
            .set_title("Enable browser connection?")
            .set_description(text)
            .set_level(rfd::MessageLevel::Warning)
            .set_buttons(rfd::MessageButtons::OkCancel)
            .show()
            .await
            == rfd::MessageDialogResult::Ok
    }
}
async fn save_file(window: &WebviewWindow) -> Option<PathBuf> {
    rfd::AsyncFileDialog::new()
        .set_parent(window)
        .set_title("Export skills")
        .set_file_name("skills.zip")
        .add_filter("Skill bundle", &["zip"])
        .save_file()
        .await
        .map(|file| file.path().to_owned())
}
pub fn copy_menu(shell: &Shell, action: &str) {
    if let Ok(menu) = shell.menu.lock() {
        if let Some((thread, task)) = &*menu {
            let text = match action {
                "copy-chat" => thread.as_deref(),
                "copy-task" => Some(task.as_str()),
                _ => None,
            };
            if let Some(text) = text {
                let _ =
                    arboard::Clipboard::new().and_then(|mut clipboard| clipboard.set_text(text));
            }
        }
    }
}

#[tauri::command]
pub async fn desktop_watch(
    window: WebviewWindow,
    shell: tauri::State<'_, Shell>,
    id: String,
    after: u64,
    conversation: Option<bool>,
) -> Result<Value, String> {
    require_main(&window)?;
    let token = CancellationToken::new();
    {
        let mut watch = shell
            .watch
            .lock()
            .map_err(|_| "Cannot start task event stream")?;
        if let Some(old) = watch.replace(token.clone()) {
            old.cancel();
        }
    }
    let api = shell.api().await?;
    let route = format!("/v1/{}/{}/events?after={after}", if conversation.unwrap_or(false) { "chats" } else { "tasks" }, segment(&id));
    let response = tokio::select! { _ = token.cancelled() => return Ok(json!({"cancelled": true})), response = api.request("GET", &route, None, None) => response? };
    window
        .emit("task-stream-connected", json!({"taskId": id, "conversationId": if conversation.unwrap_or(false) { Some(&id) } else { None }}))
        .map_err(|_| "Task window is no longer available")?;
    let mut stream = response.bytes_stream();
    let mut decoder = EventDecoder::default();
    loop {
        let chunk = tokio::select! { _ = token.cancelled() => return Ok(json!({"cancelled": true})), chunk = stream.next() => chunk };
        let Some(chunk) = chunk else {
            return Ok(json!({"cancelled": false}));
        };
        for event in decoder.push(&chunk.map_err(|_| "Task event connection failed")?)? {
            if token.is_cancelled() {
                return Ok(json!({"cancelled": true}));
            }
            window
                .emit("task-event", json!({"taskId": id, "conversationId": if conversation.unwrap_or(false) { Some(&id) } else { None }, "event": event}))
                .map_err(|_| "Task window is no longer available")?;
        }
    }
}

#[tauri::command]
pub fn desktop_unwatch(
    window: WebviewWindow,
    shell: tauri::State<'_, Shell>,
) -> Result<(), String> {
    require_main(&window)?;
    shell.cancel_watch();
    Ok(())
}
