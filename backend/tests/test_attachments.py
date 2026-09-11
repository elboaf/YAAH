"""POST /api/attachments: staging user-attached text files in the workspace."""
import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app


@pytest.fixture
def workspace_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


async def _post(workspace: Path, name: str, content: str):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/api/attachments",
            json={"workspace": str(workspace), "name": name, "content": content},
        )


@pytest.mark.asyncio
async def test_stages_file_and_returns_workspace_relative_path(workspace_dir):
    res = await _post(workspace_dir, "notes.md", "# hello\n")
    assert res.status_code == 200
    assert res.json() == {"path": ".yaah-attachments/notes.md"}
    assert (workspace_dir / ".yaah-attachments" / "notes.md").read_text(
        encoding="utf-8"
    ) == "# hello\n"


@pytest.mark.asyncio
async def test_name_collision_gets_a_counter(workspace_dir):
    assert (await _post(workspace_dir, "log.txt", "a")).status_code == 200
    assert (await _post(workspace_dir, "log.txt", "b")).status_code == 200
    folder = workspace_dir / ".yaah-attachments"
    assert {p.name for p in folder.iterdir()} == {"log.txt", "log-1.txt"}
    assert (folder / "log-1.txt").read_text(encoding="utf-8") == "b"


@pytest.mark.asyncio
async def test_rejects_binary_content(workspace_dir):
    res = await _post(workspace_dir, "blob.bin", "abc\x00def")
    assert res.status_code == 415
    assert not (workspace_dir / ".yaah-attachments").exists()


@pytest.mark.asyncio
async def test_rejects_oversized_content(workspace_dir):
    res = await _post(workspace_dir, "big.txt", "x" * (2_000_001))
    assert res.status_code == 413


@pytest.mark.asyncio
async def test_strips_path_components_from_name(workspace_dir):
    res = await _post(workspace_dir, "..\..\evil.txt", "x")
    assert res.status_code == 200
    staged = list((workspace_dir / ".yaah-attachments").iterdir())
    assert [p.name for p in staged] == ["evil.txt"]
