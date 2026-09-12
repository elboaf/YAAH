"""PyInstaller entry point for the backend sidecar.

Frozen into a standalone executable (release.yml) and launched by the
Tauri shell (src-tauri/src/lib.rs) so end users don't need Python
installed. Listens on 0.0.0.0:8765 — LAN hosting needs a non-loopback
bind, and every non-loopback request is gated behind the host
passphrase by the auth middleware in backend.main, so this is safe
even with hosting disabled (no passphrase set = all remote 401).
"""
import uvicorn

from backend.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
