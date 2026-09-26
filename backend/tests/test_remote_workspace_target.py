"""Selected remote workspace execution context stays owner-scoped."""

from backend.agent import remote as remote_mod
from backend.agent.tools import get_schemas


class StubSession:
    def __init__(self, host_id: str, windows: bool):
        self.host_id = host_id
        self.windows = windows


def test_workspace_schema_uses_its_owner_not_the_active_remote(monkeypatch):
    monkeypatch.setattr(remote_mod, "_sessions", {})
    monkeypatch.setattr(remote_mod, "_active_host_id", None)
    linux = StubSession("linux-host", False)
    windows = StubSession("windows-host", True)
    remote_mod.clear_remote()
    remote_mod.register_remote(linux, make_active=True)
    remote_mod.register_remote(windows)

    names_linux = {schema["function"]["name"] for schema in get_schemas("remote:linux-host:")}
    names_windows = {schema["function"]["name"] for schema in get_schemas("remote:windows-host:")}

    assert "powershell" not in names_linux
    assert "powershell" in names_windows
    assert "screenshot" not in names_windows
    remote_mod.clear_remote()


def test_missing_remote_workspace_host_fails_closed_in_schema_selection(monkeypatch):
    monkeypatch.setattr(remote_mod, "_sessions", {})
    monkeypatch.setattr(remote_mod, "_active_host_id", None)
    local_windows = StubSession("active-host", True)
    remote_mod.clear_remote()
    remote_mod.register_remote(local_windows, make_active=True)

    names = {schema["function"]["name"] for schema in get_schemas("remote:offline-host:")}

    assert "powershell" not in names
    assert "screenshot" not in names


def test_file_change_emission_skips_remote_namespace(monkeypatch):
    import asyncio
    from backend.agent import loop

    async def fail_if_called(_workspace):
        raise AssertionError("remote namespace must not be treated as local path")

    monkeypatch.setattr(loop.file_changes, "snapshot_workspace", fail_if_called)
    assert asyncio.run(loop._emit_file_changes(1, "remote:host-a:C:/repo", None)) is None
