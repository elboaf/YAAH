import asyncio

from backend.agent import tools


def test_get_help_lists_all_tools():
    res = asyncio.run(tools.execute_tool("get_help", {}, workspace=""))
    assert "error" not in res
    names = {line.split(":")[0][2:] for line in res["tools"].splitlines()}
    schema_names = {s["function"]["name"] for s in tools.get_schemas()}
    assert names == schema_names
    assert "get_help" in names


def test_view_image_doc_matches_same_turn_attach():
    """#175: the loop appends the image part before the next model call of
    the SAME turn; the help text must not teach NEXT-turn-only visibility."""
    doc = tools.HELP_DOCS["view_image"]
    assert "NEXT turn" not in doc
    assert "next model call" in doc


def test_get_help_unknown_tool_errors_with_hint():
    res = asyncio.run(
        tools.execute_tool("get_help", {"tool_name": "definitely_not_a_tool"}, workspace="")
    )
    assert "Unknown tool" in res["error"]
    assert "definitely_not_a_tool" in res["error"]


def test_get_help_is_read_classified():
    assert tools.tool_risk("get_help") == "read"
