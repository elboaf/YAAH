use std::process::{Child, Command};
use std::sync::Mutex;

use tauri::Manager;

/// Handle to the embedded FastAPI backend subprocess (Q26).
struct BackendHandle(Mutex<Option<Child>>);

fn python_cmd() -> &'static str {
    if cfg!(windows) {
        "python"
    } else {
        "python3"
    }
}

fn has_backend(dir: &std::path::Path) -> bool {
    dir.join("backend").join("main.py").is_file()
}

fn find_backend_cwd(app: &tauri::AppHandle) -> std::path::PathBuf {
    // `tauri dev` runs with cwd = src-tauri/, so check that dir's parent too;
    // packaged builds carry the backend somewhere under the resources dir,
    // but the exact layout depends on the bundler, so scan for it.
    let mut candidates: Vec<std::path::PathBuf> = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd);
        if let Some(parent) = candidates[0].parent() {
            candidates.push(parent.to_path_buf());
        }
    }
    if let Ok(res) = app.path().resource_dir() {
        candidates.push(res.clone());
        let mut stack = vec![(res, 0usize)];
        while let Some((dir, depth)) = stack.pop() {
            if depth < 4 {
                if let Ok(entries) = std::fs::read_dir(&dir) {
                    for e in entries.flatten() {
                        if e.path().is_dir() {
                            stack.push((e.path(), depth + 1));
                        }
                    }
                }
            }
            if has_backend(&dir) {
                candidates.push(dir);
                break;
            }
        }
    }
    candidates
        .into_iter()
        .find(|c| has_backend(c))
        .unwrap_or_else(|| std::path::PathBuf::from("."))
}

fn spawn_backend(app: &tauri::AppHandle) -> Option<Child> {
    let cwd = find_backend_cwd(app);
    // Tee backend output to a log file so startup failures are diagnosable.
    let log_path = std::env::temp_dir().join("yaah-backend.log");
    eprintln!("backend cwd: {} (log: {})", cwd.display(), log_path.display());
    let log_file = std::fs::File::create(&log_path).ok();
    let log_file_err = log_file.as_ref().and_then(|f| f.try_clone().ok());
    let mut cmd = Command::new(python_cmd());
    cmd.args(["-m", "uvicorn", "backend.main:app", "--port", "8765"])
        .current_dir(&cwd);
    if let (Some(out), Some(err)) = (log_file, log_file_err) {
        use std::process::Stdio;
        cmd.stdout(Stdio::from(out)).stderr(Stdio::from(err));
    }
    match cmd.spawn()
    {
        Ok(child) => Some(child),
        Err(e) => {
            eprintln!("failed to spawn backend: {e}");
            // Surface the failure in the UI instead of failing silently.
            if let Some(win) = app.get_webview_window("main") {
                let _ = win.eval(&format!(
                    "window.dispatchEvent(new CustomEvent('backend-error', {{detail: {}}}))",
                    serde_json::json!({ "message": format!("Failed to start backend: {e}") })
                ));
            }
            None
        }
    }
}

/// Block until the embedded backend answers /api/health (or timeout).
fn wait_for_backend(timeout: std::time::Duration) -> bool {
    let deadline = std::time::Instant::now() + timeout;
    while std::time::Instant::now() < deadline {
        if std::net::TcpStream::connect("127.0.0.1:8765").is_ok() {
            return true;
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    false
}

/// Native folder picker used by the UI to set the workspace (Q28).
#[tauri::command]
async fn pick_workspace(app: tauri::AppHandle) -> Result<String, String> {
    use tauri_plugin_dialog::DialogExt;
    let (tx, rx) = std::sync::mpsc::channel::<Option<String>>();
    app.dialog()
        .file()
        .pick_folder(move |path| {
            let _ = tx.send(path.map(|p| p.to_string()));
        });
    rx.recv()
        .ok()
        .flatten()
        .ok_or_else(|| "no folder selected".to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(BackendHandle(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();
            let child = spawn_backend(&handle);
            *app.state::<BackendHandle>().0.lock().unwrap() = child;
            // Wait for the backend to accept connections before showing the
            // webview, so early API calls don't race server startup.
            if !wait_for_backend(std::time::Duration::from_secs(15)) {
                eprintln!("backend did not become ready within 15s");
                if let Some(win) = app.get_webview_window("main") {
                    let _ = win.eval(&format!(
                        "window.dispatchEvent(new CustomEvent('backend-error', {{detail: {}}}))",
                        serde_json::json!({ "message": format!(
                            "Backend did not start within 15s. Check {} for its output.",
                            std::env::temp_dir().join("yaah-backend.log").display()
                        ) })
                    ));
                }
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![pick_workspace])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                if let Some(child) = window
                    .app_handle()
                    .state::<BackendHandle>()
                    .0
                    .lock()
                    .unwrap()
                    .as_mut()
                {
                    let _ = child.kill();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
