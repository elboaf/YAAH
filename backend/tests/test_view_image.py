"""view_image source handling: http(s) URLs (existing behavior), plus local
file sources - host absolute paths, file:/// URLs, and workspace-relative
paths (e.g. a VM screenshot written to the toolkit mount by sandbox_run).

All tests are offline: local bytes + a stubbed URL downloader.
"""
import base64

import pytest

from backend.agent import imagedata, webtools

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQAB"
    "h6FO1AAAAABJRU5ErkJggg==")


@pytest.fixture()
def images_root(tmp_path, monkeypatch):
    monkeypatch.setattr(imagedata, "IMAGES_ROOT", tmp_path / "images")
    return tmp_path / "images"


async def test_local_absolute_path_png(images_root, tmp_path):
    p = tmp_path / "shot.png"
    p.write_bytes(PNG)
    out = await webtools.view_image(str(p))
    assert "error" not in out
    assert (images_root / out["image"]).read_bytes() == PNG


async def test_home_relative_tilde_path(images_root, tmp_path, monkeypatch):
    # Path.home()/expanduser read USERPROFILE at call time on Windows
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / "vm-screen.png"
    p.write_bytes(PNG)
    out = await webtools.view_image("~/vm-screen.png")
    assert "error" not in out
    assert out["note"].startswith("Image loaded from the local file")


async def test_workspace_relative_path(images_root, tmp_path, monkeypatch):
    ws = tmp_path / "proj"
    ws.mkdir()
    (ws / "render.png").write_bytes(PNG)
    from backend.agent import tools as tools_mod

    monkeypatch.setattr(tools_mod, "workspace_root", lambda w=None: ws)
    out = await webtools.view_image("render.png", workspace=str(ws))
    assert "error" not in out
    assert (images_root / out["image"]).read_bytes() == PNG


async def test_file_url_form(images_root, tmp_path):
    p = tmp_path / "capture.png"
    p.write_bytes(PNG)
    out = await webtools.view_image(p.as_uri())  # file:///C:/Users/...
    assert "error" not in out
    assert (images_root / out["image"]).read_bytes() == PNG


async def test_missing_local_file_is_a_clean_error(images_root, tmp_path):
    out = await webtools.view_image(str(tmp_path / "nope.png"))
    assert "error" in out
    assert "not found" in out["error"]


async def test_local_non_image_bytes_rejected_by_sniff(images_root, tmp_path):
    p = tmp_path / "junk.bin"
    p.write_bytes(b"this is not an image at all")
    out = await webtools.view_image(str(p))
    assert "error" in out
    assert "not an image" in out["error"]


async def test_local_oversize_rejected(images_root, tmp_path):
    p = tmp_path / "big.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 5_000_100)
    out = await webtools.view_image(str(p))
    assert "error" in out
    assert "larger than 5 MB" in out["error"]


async def test_url_branch_still_downloads(images_root, monkeypatch):
    class FakeResp:
        headers = {"Content-Type": "image/png"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return PNG

    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout: FakeResp())
    out = await webtools.view_image("https://example.com/x.png")
    assert "error" not in out
    assert out["note"].startswith("Image downloaded from the URL above")
