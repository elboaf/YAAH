"""App version â€” the single source the backend reports about itself.

package.json is the canonical version; this file and
src-tauri/tauri.conf.json must match it at every bump (tag-on-bump.yml
fails the build if they drift). This USED to be a hardcoded
FastAPI(version="0.7.9") that silently stopped tracking releases (every
/api/remote/info reported 0.7.9 for ten versions); it lives here so the
bump can't miss it again.
"""

__version__ = "0.20.0"
