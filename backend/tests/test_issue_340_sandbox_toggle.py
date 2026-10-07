"""Issue #340: a disabled sandbox toggle must stay disabled.

The old behavior failed on three fronts, all fixed here:

1. `get_schemas()` never stripped the sandbox tools when
   ``sandbox.enabled`` was false, and `execute_tool()` had no
   dispatch-time re-check (unlike the #140 screenshot and #169 memory
   toggles). The system prompt advertised the tools either way.
2. The refusal message handed the agent the bypass recipe verbatim
   ("config.json: sandbox.enabled = true re-enables it").
3. `PUT /api/config` accepted arbitrary keys inside the ``sandbox``
   block, and nothing logged sandbox boots.

These tests pin all of them. Windows-only modules are imported lazily
inside each test so the suite still runs on non-Windows dev machines.
"""
import asyncio
import logging

from backend.agent import tools

import pytest


@pytest.fixture()
def _config(tmp_path, monkeypatch):
    """Redirect the config file (the #169/#181 dance) so tests never touch
    the developer's real settings."""
    monkeypatch.setenv("YAAH_CONFIG_PATH", str(tmp_path / "config.json"))
    import importlib

    from backend.agent import config

    saved = dict(vars(config))
    importlib.reload(config)
    yield config
    monkeypatch.delenv("YAAH_CONFIG_PATH")
    for key, value in saved.items():
        setattr(config, key, value)


def _set_enabled(config, value: bool):
    config.save_config({"sandbox": {"enabled": value}})


def test_sandbox_enabled_reads_the_toggle_live(_config):
    """The helper follows the flag as it flips, with no restart."""
    assert tools.sandbox_enabled() is True  # default
    _set_enabled(_config, False)
    assert tools.sandbox_enabled() is False
    _set_enabled(_config, True)
    assert tools.sandbox_enabled() is True


def test_sandbox_enabled_fails_open(_config, monkeypatch):
    """A broken config read must not silently amputate the tool set
    (matches screenshot_allowed()'s fail-open contract)."""

    def _boom():
        raise OSError("disk gone")

    monkeypatch.setattr(_config, "load_config", _boom)
    assert tools.sandbox_enabled() is True


@pytest.mark.skipif(tools.os.name != "nt", reason="Windows-only tool set")
class TestDisabledSandbox:
    @pytest.fixture(autouse=True)
    def _disabled(self, _config):
        """Every test in this class starts from the user's perspective:
        the sandbox toggle OFF (unlike opt-in memory, the default is ON)."""
        _set_enabled(_config, False)

    def test_schemas_strip_sandbox_tools(self, _config):
        assert tools._SANDBOX_NAMES, "sandbox schemas must be registered"
        assert not [
            n for n in tools.get_schemas()
            if n["function"]["name"] in tools._SANDBOX_NAMES
        ]

    def test_schemas_keep_sandbox_tools_when_enabled(self, _config):
        _set_enabled(_config, True)
        present = {
            n["function"]["name"] for n in tools.get_schemas()
        } & tools._SANDBOX_NAMES
        assert present == tools._SANDBOX_NAMES

    def test_dispatch_refuses_sandbox_test(self, _config):
        result = asyncio.run(
            tools.execute_tool("sandbox_test", {}, "ws")
        )
        assert result["info"].startswith(
            "The Windows Sandbox is disabled in Settings."
        )

    def test_dispatch_refusal_never_leaks_the_recipe(self, _config):
        """The regression that started #340: the old error was literally
        'config.json: sandbox.enabled = true re-enables it' — the refusal
        must never name the key or the file again."""
        for name in sorted(tools._SANDBOX_NAMES):
            result = asyncio.run(tools.execute_tool(name, {}, "ws"))
            text = str(result)
            assert "sandbox.enabled" not in text
            assert "config.json" not in text

    def test_system_prompt_omits_sandbox_section(self, _config):
        from backend.agent import loop

        prompt = loop._default_system_prompt()
        assert "# Windows Sandbox" not in prompt  # the dedicated section
        assert "sandbox_test" not in prompt       # no tool advertised

    def test_system_prompt_keeps_sandbox_section_when_enabled(self, _config):
        from backend.agent import loop

        _set_enabled(_config, True)
        prompt = loop._default_system_prompt()
        assert "Windows Sandbox" in prompt
        assert "sandbox_test" in prompt

    def test_boot_attempt_is_logged(self, _config, tmp_path, monkeypatch, caplog):
        """Validate-before-spawn: a boot is always logged with the config
        value as seen at spawn time — the audit trail #340 lacked."""


        from backend.agent import sandbox as sb

        monkeypatch.setattr(sb, "_base_dir", lambda: tmp_path / "sb")
        monkeypatch.setattr(sb, "toolkit_dir", lambda: tmp_path / "toolkit")
        monkeypatch.setattr(sb, "_SESSION", None)
        monkeypatch.setattr(sb, "_start_preview", lambda: None)
        monkeypatch.setattr(sb, "_stop_preview", lambda: None)
        monkeypatch.setattr(sb, "_sandbox_pids", list)
        _set_enabled(_config, False)
        with caplog.at_level(
            logging.WARNING, logger="backend.agent.sandbox"
        ):
            result = sb.start_sync("ws")
        assert "disabled in Settings" in result["error"]
        assert not any(
            "sandbox boot" in r.getMessage() for r in caplog.records
        ), "a disabled sandbox must never get as far as a boot line"

    def test_spawn_writes_the_boot_line(self, _config, tmp_path, monkeypatch, caplog):
        """The positive half of the audit trail: an actual spawn logs
        'sandbox boot ...' with the enabled value at spawn time."""


        from backend.agent import sandbox as sb

        monkeypatch.setattr(sb, "_cfg", lambda: {"enabled": True})
        monkeypatch.setattr(
            sb.subprocess, "Popen",
            lambda *a, **k: type("P", (), {"pid": 4242, "poll": lambda s: None})(),
        )
        with caplog.at_level(
            logging.WARNING, logger="backend.agent.sandbox"
        ):
            sb._spawn(
                tmp_path / "WindowsSandbox.exe",
                tmp_path / "s.wsb", tmp_path / "logs",
            )
        assert any(
            "sandbox boot" in r.getMessage() and "enabled=True" in r.getMessage()
            for r in caplog.records
        )


def test_config_write_flipping_the_toggle_is_logged(_config, caplog):
    """Half of #340's 'log and surface every sandbox boot attempt and every
    config write touching the sandbox block': save_config() is the one sink
    every flip path (file tools, shell, the config API) funnels through, so
    a toggle flip leaves a permanent WARNING there."""
    _set_enabled(_config, False)
    with caplog.at_level(logging.WARNING, logger="backend.agent.config"):
        _set_enabled(_config, True)
    flips = [
        r for r in caplog.records if "sandbox.enabled changed" in r.getMessage()
    ]
    assert len(flips) == 1
    assert "False -> True" in flips[0].getMessage()

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="backend.agent.config"):
        _set_enabled(_config, True)  # no-op write: same value, no flip
    assert not caplog.records
