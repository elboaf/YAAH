"""App version — the single source the backend reports about itself.

Kept in step with package.json / src-tauri/tauri.conf.json by the release
bump. This USED to be a hardcoded FastAPI(version="0.7.9") that silently
stopped tracking releases (every /api/remote/info reported 0.7.9 for ten
versions); it lives here so the bump can't miss it again.
"""

__version__ = "0.18.4"
