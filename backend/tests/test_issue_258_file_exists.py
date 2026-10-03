"""Issue #258: containment-checked existence probe for chat path links.

The frontend tokenizes path-shaped plain text in assistant messages and
probes each candidate via GET /api/files/exists. The endpoint must answer
False (never an error status) for paths that escape the workspace, so a
crafted message can't use it as an oracle for the host's filesystem, and
True only for an existing FILE inside the workspace.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app


@pytest.mark.asyncio
async def test_file_exists_probe(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.txt").write_text("alpha\n")
    (tmp_path / "d").mkdir()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # An existing file inside the workspace -> True.
        r = await c.get(
            "/api/files/exists",
            params={"workspace": str(tmp_path), "path": "sub/x.txt"},
        )
        assert r.status_code == 200
        assert r.json() == {"exists": True}

        # A directory is not a linkable file.
        r = await c.get(
            "/api/files/exists",
            params={"workspace": str(tmp_path), "path": "sub"},
        )
        assert r.json() == {"exists": False}

        # A missing sibling renders as plain text in chat.
        r = await c.get(
            "/api/files/exists",
            params={"workspace": str(tmp_path), "path": "sub/nope.txt"},
        )
        assert r.json() == {"exists": False}

        # Containment: absolute and relative escapes answer False, not 400 —
        # the probe must never distinguish "outside" from "missing".
        for evil in (
            "../outside.txt",
            "sub/../../outside.txt",
            "C:/Windows/win.ini",
            "/etc/passwd",
        ):
            r = await c.get(
                "/api/files/exists",
                params={"workspace": str(tmp_path), "path": evil},
            )
            assert r.status_code == 200
            assert r.json() == {"exists": False}


@pytest.mark.asyncio
async def test_file_exists_missing_workspace(tmp_path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(
            "/api/files/exists",
            params={"workspace": str(tmp_path / "gone"), "path": "x.txt"},
        )
        assert r.status_code == 200
        assert r.json() == {"exists": False}
