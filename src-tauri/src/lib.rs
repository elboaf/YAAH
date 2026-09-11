use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use tauri::Manager;

/// Shared control flags for the embedded FastAPI backend subprocess (Q26).
/// The child itself lives behind a Mutex so the supervisor thread owns the
/// wait/respawn loop while the window handler and `restart_backend` can
/// reach it.
struct BackendShared {
    child: Mutex<Option<Child>>,
    /// Set by the `restart_backend` command: supervisor kills + respawns.
    restart_requested: AtomicBool,
    /// Set on window destroy: supervisor stops respawning and exits.
    shutdown: AtomicBool,
}

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
    // Prefer the bundled PyInstaller sidecar (no Python needed); fall back
    // to system python for dev runs, where no sidecar exists.
    if let Some(sidecar) = find_sidecar() {
        eprintln!("using bundled backend: {}", sidecar.display());
        return spawn_with_output(&mut Command::new(sidecar), app);
    }
    let cwd = find_backend_cwd(app);
    // Tee backend output to a log file so startup failures are diagnosable.
    let log_path = std::env::temp_dir().join("yaah-backend.log");
    eprintln!("backend cwd: {} (log: {})", cwd.display(), log_path.display());
    let mut cmd = Command::new(python_cmd());
    cmd.args(["-m", "uvicorn", "backend.main:app", "--port", "8765"])
        .current_dir(&cwd);
    spawn_with_output(&mut cmd, app)
}

/// Path to the bundled backend sidecar, if this is a packaged build.
/// Tauri's externalBin copies `binaries/backend-<target-triple>[.exe]`
/// next to the main executable; accept the plain name too.
fn find_sidecar() -> Option<std::path::PathBuf> {
    // Env override for testing a locally built sidecar.
    if let Ok(p) = std::env::var("YAAH_BACKEND_EXE") {
        let p = std::path::PathBuf::from(p);
        if p.is_file() {
            return Some(p);
        }
    }
    let dir = std::env::current_exe().ok()?.parent()?.to_path_buf();
    let mut names = vec!["backend".to_string()];
    if let Some(triple) = option_env!("TAURI_ENV_TARGET_TRIPLE") {
        names.push(format!("backend-{}", triple.replace('-', "_")));
        names.push(format!("backend-{}", triple));
    }
    #[cfg(windows)]
    let names: Vec<String> = names.iter().map(|n| format!("{}.exe", n)).collect();
    names.iter().map(|n| dir.join(n)).find(|p| p.is_file())
}

/// Push a backend lifecycle event into the webview so the UI can show a
/// banner and recover (backend-status: down | up | error).
fn emit_backend_status(app: &tauri::AppHandle, status: &str, message: &str) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.eval(&format!(
            "window.dispatchEvent(new CustomEvent('backend-status', {{detail: {}}}))",
            serde_json::json!({ "status": status, "message": message })
        ));
    }
}

/// Spawn the backend command, teeing stdout/stderr to the log file,
/// hiding the console window on Windows, and surfacing spawn failures
/// in the UI.
fn spawn_with_output(cmd: &mut Command, app: &tauri::AppHandle) -> Option<Child> {
    let log_path = std::env::temp_dir().join("yaah-backend.log");
    let log_file = std::fs::File::create(&log_path).ok();
    let log_file_err = log_file.as_ref().and_then(|f| f.try_clone().ok());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
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
            emit_backend_status(app, "error", &format!("Failed to start backend: {e}"));
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

/// Run the backend for the lifetime of the app, respawning it if it crashes
/// or is restarted on request. Runs on its own thread; emits backend-status
/// events so the UI can show a banner and reload once the backend is back.
fn supervise_backend(app: tauri::AppHandle, shared: Arc<BackendShared>) {
    loop {
        if shared.shutdown.load(Ordering::SeqCst) {
            return;
        }
        let child = spawn_backend(&app);
        *shared.child.lock().unwrap() = child;
        let ready = wait_for_backend(std::time::Duration::from_secs(15));
        if ready {
            eprintln!("backend is up on 127.0.0.1:8765");
            emit_backend_status(&app, "up", "Backend is running.");
        } else {
            eprintln!("backend did not become ready within 15s");
            emit_backend_status(
                &app,
                "error",
                &format!(
                    "Backend did not start within 15s. Check {} for its output.",
                    std::env::temp_dir().join("yaah-backend.log").display()
                ),
            );
        }
        // Monitor until the process exits, a restart is requested, or the
        // app shuts down. A hung-but-alive backend is caught by the UI,
        // which calls `restart_backend` when /api/health stays unreachable.
        loop {
            if shared.shutdown.load(Ordering::SeqCst) {
                if let Some(c) = shared.child.lock().unwrap().as_mut() {
                    let _ = c.kill();
                }
                return;
            }
            if shared.restart_requested.swap(false, Ordering::SeqCst) {
                eprintln!("restart_backend requested; killing backend");
                if let Some(mut c) = shared.child.lock().unwrap().take() {
                    let _ = c.kill();
                    let _ = c.wait();
                }
                break;
            }
            let exited = shared
                .child
                .lock()
                .unwrap()
                .as_mut()
                .and_then(|c| c.try_wait().ok().flatten());
            if let Some(status) = exited {
                if shared.shutdown.load(Ordering::SeqCst) {
                    return;
                }
                eprintln!("backend exited ({status}); respawning in 1s");
                emit_backend_status(&app, "down", "Backend crashed — restarting…");
                std::thread::sleep(std::time::Duration::from_secs(1));
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(200));
        }
    }
}

/// Kill and respawn the backend (used by the UI when it finds the backend
/// unreachable but wants recovery without an app restart).
#[tauri::command]
fn restart_backend(shared: tauri::State<'_, Arc<BackendShared>>) -> Result<(), String> {
    shared.restart_requested.store(true, Ordering::SeqCst);
    Ok(())
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
        .manage(Arc::new(BackendShared {
            child: Mutex::new(None),
            restart_requested: AtomicBool::new(false),
            shutdown: AtomicBool::new(false),
        }))
        .setup(|app| {
            let handle = app.handle().clone();
            let shared = app.state::<Arc<BackendShared>>().inner().clone();
            std::thread::spawn(move || supervise_backend(handle, shared));
            // Wait for the backend to accept connections before showing the
            // webview, so early API calls don't race server startup. The
            // supervisor emits backend-status events either way.
            wait_for_backend(std::time::Duration::from_secs(15));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![pick_workspace, restart_backend])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                let shared = window.app_handle().state::<Arc<BackendShared>>();
                shared.shutdown.store(true, Ordering::SeqCst);
                let mut guard = shared.child.lock().unwrap();
                if let Some(child) = guard.as_mut() {
                    let _ = child.kill();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
