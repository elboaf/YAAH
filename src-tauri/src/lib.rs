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

fn spawn_backend(app: &tauri::AppHandle) -> Option<Child> {
    // Run uvicorn from the project root. In dev this is the repo root; for
    // packaged builds the resources dir carries the backend sources.
    let cwd = app
        .path()
        .resource_dir()
        .ok()
        .filter(|p| p.join("backend").exists())
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    match Command::new(python_cmd())
        .args(["-m", "uvicorn", "backend.main:app", "--port", "8765"])
        .current_dir(&cwd)
        .spawn()
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
