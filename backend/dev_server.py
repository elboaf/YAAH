"""Dev-entry wrapper for `npm run dev:backend`.

The packaged app resolves its data to ~/.yaah; a bare `uvicorn
backend.main:app` from the repo does not, so it silently served an empty
repo-local database — and when both ran at once, the app could end up
talking to the dev backend on 8765 and show no chat history. Pin dev runs
to a repo-local DB and config up front so a dev server can never touch
(or impersonate) live data.
"""
import os
from pathlib import Path

_repo_data = Path(__file__).resolve().parent / "data"
os.environ.setdefault("YAAH_DB_PATH", str(_repo_data / "dev-agent.db"))
os.environ.setdefault("YAAH_CONFIG_PATH", str(_repo_data / "dev-config.json"))

import uvicorn

if __name__ == "__main__":
    uvicorn.run("backend.main:app", reload=True, port=8765)
