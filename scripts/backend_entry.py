"""PyInstaller entry point for the backend sidecar.

Frozen into a standalone executable (release.yml) and launched by the
Tauri shell (src-tauri/src/lib.rs) so end users don't need Python
installed. Listens on the same 127.0.0.1:8765 the app expects.
"""
import uvicorn

from backend.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")
