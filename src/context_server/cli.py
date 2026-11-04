"""Command-line access to the same tools the MCP server exposes, for agents that run shell commands."""
import argparse
import asyncio
import json
import sys
from importlib.resources import files
from pathlib import Path

from mcp.server.fastmcp.exceptions import ToolError

from .server import create_server, db_path


def kind(schema: dict) -> str:
    return schema.get("type") or next(s["type"] for s in schema["anyOf"] if s["type"] != "null")


def parse(schema: dict):
    def convert(value: str):
        match kind(schema):
            case "integer":
                return int(value)
            case "object" | "array":
                return json.loads(value)
        return sys.stdin.read() if value == "-" else value
    return convert


def skill_text() -> str:
    package = files("context_server")
    return package.joinpath("skill.md").read_text() + "\n" + package.joinpath("guide.md").read_text()


def build_parser(tools) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-context", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install-skill", help="write the agent-context skill into a skills directory")
    install.add_argument("--dir", type=Path, default=Path.home() / ".claude" / "skills")
    for tool in tools:
        sub = commands.add_parser(tool.name, help=tool.description.splitlines()[0], description=tool.description)
        required = set(tool.inputSchema.get("required", []))
        for name, schema in tool.inputSchema["properties"].items():
            hint = {"object": "JSON object", "array": "JSON array"}.get(kind(schema), kind(schema))
            sub.add_argument(f"--{name}", dest=name, required=name in required, type=parse(schema),
                             help=hint + ("; '-' reads stdin" if hint == "string" else ""))
    return parser


def main(argv: list[str] | None = None) -> int:
    server = create_server(db_path())
    tools = asyncio.run(server.list_tools())
    args = vars(build_parser(tools).parse_args(argv))
    command = args.pop("command")
    if command == "install-skill":
        target = args["dir"].expanduser() / "agent-context" / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(skill_text())
        print(target)
        return 0
    try:
        _, structured = asyncio.run(server.call_tool(command, {k: v for k, v in args.items() if v is not None}))
    except ToolError as exc:
        print(exc, file=sys.stderr)
        return 1
    result = structured["result"] if set(structured) == {"result"} else structured
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0
