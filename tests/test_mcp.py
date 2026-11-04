import json
import os
import shutil
from pathlib import Path

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

pytestmark = pytest.mark.anyio


async def test_launch_from_mcp_config(tmp_path):
    """The real launch path; tool behaviour is covered in-process by test_server.py."""
    config = json.loads((Path(__file__).parents[1] / "mcp-config.json").read_text())["mcpServers"]["context"]
    args = config["args"].copy()
    args[args.index("--directory") + 1] = str(Path(__file__).resolve().parents[1])
    uv = shutil.which("uv")
    assert uv, "uv is required for the MCP launch test"
    params = StdioServerParameters(command=uv, args=args,
        env={**os.environ, "CONTEXT_DB": str(tmp_path / "mcp.db")})
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert len(tools) == 11
            assert tools["infer"].annotations.readOnlyHint
            assert tools["delete_record"].annotations.destructiveHint
            guide = await client.read_resource("context://guide")
            assert "unknown" in guide.contents[0].text
            added = await client.call_tool("put_note", {"space": "test", "id": "n1",
                "title": "Decision", "content": "SQLite chosen", "source": "test:meeting"})
            assert not added.isError
            found = await client.call_tool("search_notes", {"space": "test", "query": "SQLite"})
            assert not found.isError and "SQLite chosen" in found.content[0].text
