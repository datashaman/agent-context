"""In-process tests for the MCP tool layer and the reasoner (test_mcp.py covers the real stdio launch)."""
import io
import json
import runpy
import sys

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from context_server import server
from context_server.reasoner import solve


@pytest.fixture
def mcp(tmp_path):
    return server.create_server(tmp_path / "context.db")


async def call(mcp, name, **args):
    _, structured = await mcp.call_tool(name, args)
    return structured.get("result", structured) if set(structured) == {"result"} else structured


async def test_every_tool_round_trips(mcp):
    tools = {t.name: t for t in await mcp.list_tools()}
    assert set(tools) == {"put_note", "get_note", "search_notes", "list_notes", "list_spaces", "put_fact", "list_facts",
                          "put_rule", "list_rules", "infer", "delete_record"}
    read_only = {n for n, t in tools.items() if t.annotations.readOnlyHint}
    assert read_only == {"get_note", "search_notes", "list_notes", "list_spaces", "list_facts", "list_rules", "infer"}

    await call(mcp, "put_note", space="s", id="n1", title="DB", content="Use SQLite", source="m",
               metadata={"by": "Sam"}, expires_at="2099-01-01T00:00:00Z")
    note = await call(mcp, "get_note", space="s", id="n1")
    assert note["metadata"] == {"by": "Sam"} and note["expires_at"].startswith("2099-01-01")
    assert [n["id"] for n in await call(mcp, "search_notes", space="s", query="sqlite")] == ["n1"]
    assert [n["id"] for n in await call(mcp, "list_notes", space="s")] == ["n1"]
    assert await call(mcp, "list_spaces") == [{"space": "s", "notes": 1, "facts": 0, "rules": 0}]

    for id, subject, obj in [("f1", "a", "b"), ("f2", "b", "c")]:
        await call(mcp, "put_fact", space="s", id=id, subject=subject, predicate="dep", object=obj, source="r")
    assert [f["id"] for f in await call(mcp, "list_facts", space="s", subject="b")] == ["f2"]
    await call(mcp, "put_rule", space="s", id="t", head=["?x", "dep", "?z"],
               body=[["?x", "dep", "?y"], ["?y", "dep", "?z"]], source="p")
    assert (await call(mcp, "list_rules", space="s"))[0]["definition"]["head"] == ["?x", "dep", "?z"]
    inferred = await call(mcp, "infer", space="s", subject="a", object="c")
    assert inferred["total_matches"] == 1 and inferred["facts"][0]["derived"]

    assert (await call(mcp, "delete_record", space="s", kind="note", id="n1"))["deleted"] is True
    assert await call(mcp, "get_note", space="s", id="n1") is None


async def test_tool_errors_surface(mcp):
    with pytest.raises(ToolError, match="kind must be"):
        await mcp.call_tool("delete_record", {"space": "s", "kind": "table", "id": "x"})
    with pytest.raises(ToolError, match="limit"):
        await mcp.call_tool("list_notes", {"space": "s", "limit": 500})


async def test_guide_resource_states_trust_rules(mcp):
    [content] = await mcp.read_resource("context://guide")
    for phrase in ("untrusted", "list_spaces", "list_notes", "after", "unknown"):
        assert phrase in content.content


class FakeServer:
    def __init__(self, path):
        self.path = path
        FakeServer.last = self

    def run(self, transport):
        self.transport = transport


def test_main_uses_context_db_env_over_stdio(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXT_DB", str(tmp_path / "env.db"))
    monkeypatch.setattr(server, "create_server", FakeServer)
    server.main()
    assert (FakeServer.last.path, FakeServer.last.transport) == (str(tmp_path / "env.db"), "stdio")


def test_main_defaults_to_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("CONTEXT_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "create_server", FakeServer)
    server.main()
    assert FakeServer.last.path == str(tmp_path / "context.db")


def fact(s, p, o):
    return {"subject": s, "predicate": p, "object": o}


def rule(head, body):
    return {"definition": json.dumps({"head": head, "body": body})}


def test_solve_closure_constants_and_no_rules():
    facts = [fact("a", "p", "b"), fact("b", "p", "c")]
    assert solve({"facts": facts, "rules": []}) == [["a", "p", "b"], ["b", "p", "c"]]
    tagged = rule(["?x", "kind", "service"], [["?x", "p", "?y"]])
    assert ["a", "kind", "service"] in solve({"facts": facts, "rules": [tagged]})
    assert solve({"facts": [], "rules": []}) == []


def test_solve_rejects_invalid_stored_rule_and_oversized_domain():
    with pytest.raises(ValueError, match="variables"):
        solve({"facts": [], "rules": [rule(["?x", "p", "?y"], [["?x", "p", "b"]])]})
    with pytest.raises(ValueError, match="128"):
        solve({"facts": [fact(str(i), "p", "o") for i in range(130)], "rules": []})


def test_solve_rejects_huge_closure():
    # 102 terms and 2 variables fit the grounding budget, but 100*100 pairs + 100 facts > 10000 triples.
    facts = [fact(str(i), "p", "x") for i in range(99)] + [fact("x", "p", "x")]
    pairs = rule(["?a", "q", "?b"], [["?a", "p", "x"], ["?b", "p", "x"]])
    with pytest.raises(ValueError, match="10000"):
        solve({"facts": facts, "rules": [pairs]})


def run_reasoner(monkeypatch, capsys, stdin):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    monkeypatch.delitem(sys.modules, "context_server.reasoner", raising=False)
    try:
        runpy.run_module("context_server.reasoner", run_name="__main__")
        code = 0
    except SystemExit as exc:
        code = exc.code
    return code, capsys.readouterr()


def test_reasoner_entrypoint_success_and_failure(monkeypatch, capsys):
    code, out = run_reasoner(monkeypatch, capsys, json.dumps({"facts": [fact("a", "p", "b")], "rules": []}))
    assert code == 0 and json.loads(out.out) == [["a", "p", "b"]]
    code, out = run_reasoner(monkeypatch, capsys, "not json")
    assert code == 1 and out.out == "" and out.err


def test_solve_rejects_grounding_blowup():
    # 101+ terms and 3 body variables: 101**3 > 1,000,000 estimated assignments.
    three_vars = rule(["?a", "r", "?c"], [["?a", "p", "?b"], ["?b", "p", "?c"]])
    with pytest.raises(ValueError, match="grounding estimate"):
        solve({"facts": [fact(str(i), "p", "o") for i in range(100)], "rules": [three_vars]})
