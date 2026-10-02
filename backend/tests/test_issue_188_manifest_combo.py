"""Issue #188, harness combo: with the fixture remote host owning the
workspace, the rendered sub-agent prompt and its schema derivation must
describe the SAME host — env line from the fixture (Windows) host, tool
sentence derived from that host's schema resolution (powershell present),
never from the legacy active host or the client's os.name."""
import pytest

from backend.agent import prompt_manifest as pm
from backend.agent import remote as remote_mod

PFX = "win" if pm.HOST_WINDOWS else "posix"
HOST_WIN = pm.HOST_WINDOWS


@pytest.fixture
def fixture_host_owning_workspace(monkeypatch):
    """The manifest harness's fixture host registered, with the sub-agent
    workspace namespaced to it."""
    pm._install_fixture_host()
    yield pm
    remote_mod.clear_remote()


@pytest.mark.skipif(
    not HOST_WIN,
    reason="env-line shell wording is asserted for the win fixture host",
)
def test_subagent_prompt_and_schemas_agree_on_fixture_host(
    fixture_host_owning_workspace,
):
    pm_mod = fixture_host_owning_workspace
    from backend.agent.subagents import get_agent_def, _resolve_tools, _sub_agent_system_prompt

    defn = get_agent_def("general-purpose")
    prompt = _sub_agent_system_prompt(defn, pm_mod.REMOTE_WS)
    resolved = {
        s["function"]["name"]
        for s in _resolve_tools(defn, workspace=pm_mod.REMOTE_WS)
    }
    # Same host on both sides: the Windows fixture host offers powershell,
    # and the env line names its workspace and shell.
    assert "powershell" in resolved
    assert "powershell" in prompt
    assert "C:/fixture/project on the host" in prompt
    assert "Git Bash" in prompt
