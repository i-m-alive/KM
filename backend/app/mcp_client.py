"""Client side of naviknow-mcp.

Spawns the MCP server (mcp_server.server) as a stdio subprocess and provides an
async session for calling its tools. Used by the worker/agents during the
Bedrock tool-use loop.
"""

import os
import sys
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# backend/ directory — the cwd the server must run in so `app` and
# `mcp_server` are importable.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Tool names as registered by FastMCP (function names in mcp_server.server).
TOOL_READ_DOCUMENT = "fs_read_document"
TOOL_CREATE_REVIEW = "review_create_task"


@asynccontextmanager
async def mcp_session():
    """Open a connected MCP ClientSession for the duration of the context."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=_BACKEND_DIR,
        env=os.environ.copy(),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_tool_text(session: ClientSession, name: str, arguments: dict) -> str:
    """Call an MCP tool and return its concatenated text output."""
    result = await session.call_tool(name, arguments)
    parts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    return "".join(parts)


async def list_tool_schemas(session: ClientSession) -> list[dict]:
    """Return tool schemas in Bedrock Converse toolSpec shape, for the tool-use loop."""
    tools = await session.list_tools()
    specs = []
    for t in tools.tools:
        specs.append(
            {
                "toolSpec": {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": {"json": t.inputSchema},
                }
            }
        )
    return specs
