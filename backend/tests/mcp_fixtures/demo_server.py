"""Tiny stdio MCP server (mcp 2.x) for the demo/integration test."""
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("demo")

@mcp.tool()
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo: {text}"

@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

mcp.run()
