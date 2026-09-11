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
    // The resource dir (marker: backend/main.py) is the cwd for BOTH spawn
    // paths: the sidecar resolves bundled assets relative to it (whisper
    // engine/model live under backend/whisper/), and without it a Start
    // Menu launch inherits something like C:\Windows\System32 and finds
    // nothing.
    let cwd = find_backend_cwd(app);
    // Prefer the bundled PyInstaller sidecar (no Python needed); fall back
    // to system python for dev runs, where no sidecar exists.
    if let Some(sidecar) = find_sidecar() {
        eprintln!(
            "using bundled backend: {} (cwd: {})",
            sidecar.display(),
            cwd.display()
        );
        let mut cmd = Command::new(sidecar);
        cmd.current_dir(&cwd);
        return spawn_with_output(&mut cmd, app);
    }
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
    // Append, not truncate: a respawn sequence is only diagnosable if the
    // previous attempts' output is still there.
    let log_file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .ok();
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

/// Kill the backend and its whole process tree. A bare `child.kill()` is
/// not enough for the PyInstaller --onefile sidecar: the spawned exe is a
/// bootloader that runs the real server as a child process, so killing the
/// bootloader orphans the actual backend (which keeps port 8765 busy and
/// turns the next launch into a respawn loop).
fn kill_tree(child: &mut Child) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let _ = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .creation_flags(0x0800_0000)
            .output();
    }
    #[cfg(not(windows))]
    {
        let _ = child.kill();
    }
    let _ = child.wait();
}

/// Best-effort clear a stale backend left holding port 8765 by an earlier
/// app instance (see kill_tree: a bare kill used to orphan it). Only safe
/// because `backend.exe` is our own sidecar's image name.
fn clear_stale_port_holder() {
    if !std::net::TcpStream::connect("127.0.0.1:8765").is_ok() {
        return;
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let _ = Command::new("taskkill")
            .args(["/IM", "backend.exe", "/T", "/F"])
            .creation_flags(0x0800_0000)
            .output();
    }
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while std::time::Instant::now() < deadline {
        if !std::net::TcpStream::connect("127.0.0.1:8765").is_ok() {
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(200));
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

/// Wait for OUR child to be the thing serving the port. A bare port check
/// is not enough: a foreign/stale backend holding 8765 makes connect()
/// succeed while our freshly spawned child dies with a bind error — that
/// false "up" once flapped the supervisor for a dozen spawns. Returns
/// Ok(true) only while the child is alive AND the port answers; Ok(false)
/// when the child exited (foreign holder) or the deadline passed. Locks are
/// taken per poll so a shutdown during startup is never blocked out.
fn wait_for_own_backend(
    shared: &BackendShared,
    timeout: std::time::Duration,
) -> std::io::Result<bool> {
    let deadline = std::time::Instant::now() + timeout;
    loop {
        if shared.shutdown.load(Ordering::SeqCst) {
            return Ok(false);
        }
        let exited = shared
            .child
            .lock()
            .unwrap()
            .as_mut()
            .and_then(|c| c.try_wait().ok().flatten());
        let port_up = std::net::TcpStream::connect("127.0.0.1:8765").is_ok();
        if port_up {
            return Ok(exited.is_none());
        }
        if exited.is_some() {
            return Ok(false);
        }
        if std::time::Instant::now() >= deadline {
            return Ok(false);
        }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
}

/// Run the backend for the lifetime of the app, respawning it if it crashes
/// or is restarted on request. Runs on its own thread; emits backend-status
/// events so the UI can show a banner and reload once the backend is back.
fn supervise_backend(app: tauri::AppHandle, shared: Arc<BackendShared>) {
    // A previous app instance may have left an orphaned backend holding
    // the port (onefile bootloader kill bug); clear it before first spawn.
    clear_stale_port_holder();
    // Consecutive exits within 30s of spawn: a backend that dies instantly
    // (port taken, bad config, missing module) would otherwise be respawned
    // forever, flapping the UI. 30s because a cold PyInstaller onefile start
    // (AV scan + extraction) can easily outlive a 5s window and still be a
    // failure. After 3 we park until restart_backend.
    let mut fast_deaths = 0u32;
    loop {
        if shared.shutdown.load(Ordering::SeqCst) {
            return;
        }
        let spawned_at = std::time::Instant::now();
        *shared.child.lock().unwrap() = spawn_backend(&app);
        // Wait for OUR child to serve the port. A bare port check is not
        // enough: a foreign/stale backend on 8765 makes connect() succeed
        // while our child dies with a bind error — that false "up" once
        // flapped this loop for a dozen spawns.
        let ready = wait_for_own_backend(&shared, std::time::Duration::from_secs(15));
        match ready {
            Ok(true) => {
                eprintln!("backend is up on 127.0.0.1:8765");
                emit_backend_status(&app, "up", "Backend is running.");
            }
            Ok(false) if std::net::TcpStream::connect("127.0.0.1:8765").is_ok() => {
                // The port answers but our child is gone: a foreign/stale
                // backend owns 8765. Clear it before respawning, or every
                // respawn dies on the bind error.
                eprintln!("backend died while port 8765 stayed up; clearing stale holder");
                emit_backend_status(&app, "down", "Stale backend found on port — clearing…");
                clear_stale_port_holder();
            }
            _ => {
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
        }
        // Monitor until the process exits, a restart is requested, or the
        // app shuts down. A hung-but-alive backend is caught by the UI,
        // which calls `restart_backend` when /api/health stays unreachable.
        loop {
            if shared.shutdown.load(Ordering::SeqCst) {
                let mut guard = shared.child.lock().unwrap();
                if let Some(c) = guard.as_mut() {
                    kill_tree(c);
                }
                return;
            }
            if shared.restart_requested.swap(false, Ordering::SeqCst) {
                eprintln!("restart_backend requested; killing backend");
                let mut guard = shared.child.lock().unwrap();
                if let Some(c) = guard.as_mut() {
                    kill_tree(c);
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
                fast_deaths = if spawned_at.elapsed() < std::time::Duration::from_secs(30) {
                    fast_deaths + 1
                } else {
                    0
                };
                emit_backend_status(&app, "down", "Backend crashed — restarting…");
                if fast_deaths >= 3 {
                    // Stop the respawn loop: flapping "crashed/recovered"
                    // reloads is worse than an honest error. Park until the
                    // UI (or a config fix) requests a restart.
                    eprintln!("backend exited instantly {fast_deaths}x ({status}); parking");
                    emit_backend_status(
                        &app,
                        "error",
                        "Backend keeps crashing on startup. Its output is in yaah-backend.log in your TEMP folder.",
                    );
                    loop {
                        if shared.shutdown.load(Ordering::SeqCst) {
                            return;
                        }
                        if shared.restart_requested.swap(false, Ordering::SeqCst) {
                            fast_deaths = 0;
                            // A common cause of instant deaths is a stale
                            // backend from another app instance holding the
                            // port; give it one clear attempt.
                            clear_stale_port_holder();
                            break;
                        }
                        std::thread::sleep(std::time::Duration::from_millis(300));
                    }
                } else {
                    eprintln!("backend exited ({status}); respawning in 1s");
                    std::thread::sleep(std::time::Duration::from_secs(1));
                }
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
                // kill_tree, not child.kill(): the PyInstaller onefile
                // bootloader runs the real server as a child, and a bare
                // kill orphans it — the next launch then starts with a
                // stale port holder and a respawn cascade.
                let mut guard = shared.child.lock().unwrap();
                if let Some(child) = guard.as_mut() {
                    kill_tree(child);
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
