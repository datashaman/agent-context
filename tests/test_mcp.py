import json
import os
import shutil
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_real_stdio_client(tmp_path):
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
            assert len(tools) == 9
            assert tools["infer"].annotations.readOnlyHint
            assert tools["delete_record"].annotations.destructiveHint
            guide = await client.read_resource("context://guide")
            assert "unknown" in guide.contents[0].text
            added = await client.call_tool("put_note", {"space": "test", "id": "n1",
                "title": "Decision", "content": "SQLite chosen", "source": "test:meeting"})
            assert not added.isError
            found = await client.call_tool("search_notes", {"space": "test", "query": "SQLite"})
            assert not found.isError and "SQLite chosen" in found.content[0].text
            invalid = await client.call_tool("put_fact", {"space": "", "id": "f1",
                "subject": "a", "predicate": "p", "object": "b", "source": "s"})
            assert invalid.isError
            for id, subject, object in [("f1", "a", "b"), ("f2", "b", "c")]:
                result = await client.call_tool("put_fact", {"space": "test", "id": id,
                    "subject": subject, "predicate": "depends", "object": object, "source": "s"})
                assert not result.isError
            rule = await client.call_tool("put_rule", {"space": "test", "id": "transitive",
                "head": ["?x", "depends", "?z"], "body": [["?x", "depends", "?y"],
                ["?y", "depends", "?z"]], "source": "policy"})
            assert not rule.isError
            result = await client.call_tool("infer", {"space": "test", "subject": "a", "object": "c"})
            assert not result.isError
            assert result.structuredContent["total_matches"] == 1
            assert result.structuredContent["facts"][0]["derived"] is True
