import asyncio

from backend.agent import tools


def test_get_help_lists_all_tools():
    res = asyncio.run(tools.execute_tool("get_help", {}, workspace=""))
    assert "error" not in res
    names = {line.split(":")[0][2:] for line in res["tools"].splitlines()}
    schema_names = {s["function"]["name"] for s in tools.get_schemas()}
    assert names == schema_names
    assert "get_help" in names


def test_get_help_known_tool_returns_schema_and_notes():
    res = asyncio.run(
        tools.execute_tool("get_help", {"tool_name": "screenshot"}, workspace="")
    )
    if tools.os.name != "nt":
        # Computer-use tools don't exist off-Windows; get_help must say so.
        assert "Unknown tool" in res["error"]
        return
    assert res["name"] == "screenshot"
    assert "properties" in res["schema"]
    # The trimmed screenshot caveats live in the help notes.
    assert "ruler" in res["notes"]


def test_get_help_unknown_tool_errors_with_hint():
    res = asyncio.run(
        tools.execute_tool("get_help", {"tool_name": "mouse_clickx"}, workspace="")
    )
    assert "Unknown tool" in res["error"]
    assert "mouse_click" in res["error"]


def test_get_help_is_read_classified():
    assert tools.tool_risk("get_help") == "read"
