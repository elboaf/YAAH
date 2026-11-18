"""Packaging regression tests: Tauri resource lists must ship the pinned
Interceptor manifest.

#759 taught the backend to discover ``interceptor_manifest.json`` in the
packaged layout, but that only works if the bundler actually packs the
file. Each Tauri config carries its own ``bundle.resources`` list -- #762
shipped because the Windows override silently lacked the manifest line
while the main config had it. These tests pin every resources list in the
repo, so the manifest cannot be dropped (or a new platform config added
without it) again.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TAURI_DIR = REPO_ROOT / "src-tauri"

REQUIRED_RESOURCE = "../backend/agent/interceptor_manifest.json"


def _resources_lists():
    """(config path, resources list) for every Tauri config declaring one."""
    lists = []
    for conf in sorted(TAURI_DIR.glob("tauri*.conf.json")):
        data = json.loads(conf.read_text(encoding="utf-8"))
        resources = (data.get("bundle") or {}).get("resources")
        if resources is not None:
            lists.append((conf, resources))
    return lists


def test_tauri_configs_declaring_resources_are_pinned():
    names = {conf.relative_to(REPO_ROOT).as_posix() for conf, _ in _resources_lists()}
    # If this set drifts (a config stops declaring resources or stops
    # parsing), the manifest pin below would silently cover less.
    assert names == {
        "src-tauri/tauri.conf.json",
        "src-tauri/tauri.windows.conf.json",
    }


def test_every_resources_list_ships_interceptor_manifest():
    for conf, resources in _resources_lists():
        assert REQUIRED_RESOURCE in resources, (
            f"{conf.relative_to(REPO_ROOT)} bundle.resources is missing "
            f"{REQUIRED_RESOURCE}: packaged builds fail closed on the "
            f"Install button (#758, #762)"
        )


def test_manifest_file_exists_in_repo():
    manifest = REPO_ROOT / "backend" / "agent" / "interceptor_manifest.json"
    assert manifest.is_file()
    assert isinstance(json.loads(manifest.read_text(encoding="utf-8")), dict)
