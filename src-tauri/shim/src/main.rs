//! yaah-update-shim: the update handoff shim (#16).
//!
//! Spawned by the `prepare_update` Tauri command right before the UI destroys
//! its window. It must outlive the app, so it is deliberately std-only — no
//! tauri, no tokio, nothing that makes this binary heavy or slow to build.
//!
//! Handoff sequence:
//!   1. wait for the YAAH process (by PID) to exit — the window's
//!      `on_window_event(Destroyed)` handler kill_tree()s the embedded
//!      backend on the way down;
//!   2. wait for port 8765 to go free — a bare kill used to orphan the
//!      PyInstaller onefile backend (see lib.rs kill_tree), and the NSIS
//!      installer fails to replace files while anything holds them;
//!   3. run the downloaded installer (interactive NSIS UI) and exit with its
//!      code.
//!
//! If the app never exits within the budget, the shim gives up WITHOUT
//! running the installer — a half-dead app must never be replaced underneath
//! itself.
//!
//! Windows note: tasklist is used rather than OpenProcess so this stays
//! pure-std (no winapi dep); children run with CREATE_NO_WINDOW so no
//! console flashes during the handoff.

use std::net::TcpStream;
use std::process::Command;
use std::thread::sleep;
use std::time::{Duration, Instant};

const PID_EXIT_BUDGET: Duration = Duration::from_secs(30);
const PORT_FREE_BUDGET: Duration = Duration::from_secs(30);
const POLL: Duration = Duration::from_millis(200);
/// CREATE_NO_WINDOW — the shim is fully detached; children must not flash a
/// console either.
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

struct Args {
    pid: u32,
    installer: String,
    /// Backend port to wait on; 0 disables the port-free wait (none in dev).
    port: u16,
}

fn parse_args() -> Option<Args> {
    let mut pid: Option<u32> = None;
    let mut installer: Option<String> = None;
    let mut port: u16 = 8765;
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--pid" => pid = it.next().and_then(|v| v.parse().ok()),
            "--installer" => installer = it.next(),
            "--port" => port = it.next().and_then(|v| v.parse().ok()).unwrap_or(0),
            _ => {}
        }
    }
    Some(Args {
        pid: pid?,
        installer: installer?,
        port,
    })
}

/// Is the given PID still running? Best-effort: on any probe failure we
/// assume alive (keeps us on the safe side of the "never install under a
/// live app" rule).
fn pid_alive(pid: u32) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/FO", "CSV", "/NH"])
            .creation_flags(CREATE_NO_WINDOW)
            .output()
            .map(|o| String::from_utf8_lossy(&o.stdout).contains(&pid.to_string()))
            .unwrap_or(true)
    }
    #[cfg(not(windows))]
    {
        std::path::Path::new(&format!("/proc/{pid}")).exists()
    }
}

/// Is anything still listening on the backend port? Connect success = busy.
fn port_busy(port: u16) -> bool {
    TcpStream::connect(("127.0.0.1", port)).is_ok()
}

fn wait_until<F: Fn() -> bool>(budget: Duration, done: F) -> bool {
    let deadline = Instant::now() + budget;
    loop {
        if done() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        sleep(POLL);
    }
}

fn main() {
    let args = match parse_args() {
        Some(a) => a,
        None => {
            eprintln!("usage: yaah-update-shim --pid <n> --installer <path> [--port <n>]");
            std::process::exit(64);
        }
    };

    // 1. App must actually exit before we touch its files.
    if !wait_until(PID_EXIT_BUDGET, || !pid_alive(args.pid)) {
        eprintln!("yaah-update-shim: app pid {} still alive, aborting", args.pid);
        std::process::exit(2);
    }

    // 2. Embedded backend must release the port (kill_tree is async-ish).
    if args.port != 0 && !wait_until(PORT_FREE_BUDGET, || !port_busy(args.port)) {
        eprintln!("yaah-update-shim: port {} still busy, aborting", args.port);
        std::process::exit(3);
    }

    // 3. Hand off to the installer and mirror its exit code.
    if !std::path::Path::new(&args.installer).is_file() {
        eprintln!("yaah-update-shim: installer missing: {}", args.installer);
        std::process::exit(4);
    }
    let status = Command::new(&args.installer)
        .status()
        .expect("installer spawn failed");
    std::process::exit(status.code().unwrap_or(0));
}
