"""A minimal stdio MCP server used by the tests."""

import os

from mcp.server.mcpserver import MCPServer

server = MCPServer("echo")


@server.tool()
def shout(text: str) -> str:
    """Return the text in upper case, prefixed with the PREFIX environment variable."""
    return os.environ.get("PREFIX", "") + text.upper()


if __name__ == "__main__":
    server.run("stdio")
