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
    Command::new(python_cmd())
        .args(["-m", "uvicorn", "backend.main:app", "--port", "8765"])
        .current_dir(&cwd)
        .spawn()
        .map_err(|e| eprintln!("failed to spawn backend: {e}"))
        .ok()
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
    rx.recv().map_err(|e| e.to_string())
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
