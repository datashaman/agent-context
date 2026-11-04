import io
import json

import pytest

from context_server import cli, server


@pytest.fixture(autouse=True)
def database(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXT_DB", str(tmp_path / "cli.db"))


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, json.loads(out.out) if code == 0 and out.out.strip() else out.err


def test_every_server_tool_is_a_command(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    usage = capsys.readouterr().out
    for name in ("list_spaces", "put_note", "search_notes", "list_notes", "infer", "delete_record", "install-skill"):
        assert name in usage


def test_notes_round_trip_with_stdin_and_json_metadata(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("Use SQLite\nfor local storage"))
    code, result = run(capsys, "put_note", "--space", "demo", "--id", "db", "--title", "Database",
                       "--source", "adr:3", "--content", "-", "--metadata", '{"by": "Sam"}')
    assert code == 0 and result == {"id": "db", "space": "demo"}
    _, found = run(capsys, "search_notes", "--space", "demo", "--query", "sqlite", "--limit", "5")
    assert found[0]["content"] == "Use SQLite\nfor local storage" and found[0]["metadata"] == {"by": "Sam"}
    _, spaces = run(capsys, "list_spaces")
    assert spaces == [{"space": "demo", "notes": 1, "facts": 0, "rules": 0}]
    _, missing = run(capsys, "get_note", "--space", "demo", "--id", "nope")
    assert missing is None


def test_rules_take_json_and_infer_derives(capsys):
    for id, s, o in [("f1", "a", "b"), ("f2", "b", "c")]:
        run(capsys, "put_fact", "--space", "d", "--id", id, "--subject", s, "--predicate", "dep",
            "--object", o, "--source", "s")
    code, _ = run(capsys, "put_rule", "--space", "d", "--id", "t", "--source", "p",
                  "--head", '["?x","dep","?z"]', "--body", '[["?x","dep","?y"],["?y","dep","?z"]]')
    assert code == 0
    _, result = run(capsys, "infer", "--space", "d", "--subject", "a", "--object", "c")
    assert result["total_matches"] == 1 and result["facts"][0]["derived"] is True


def test_errors_exit_nonzero_on_stderr(capsys):
    code, err = run(capsys, "delete_record", "--space", "d", "--kind", "table", "--id", "x")
    assert code == 1 and "kind must be" in err
    with pytest.raises(SystemExit):
        cli.main(["put_note", "--space", "d"])  # missing required flags
    assert "required" in capsys.readouterr().err


def test_install_skill_bundles_guide(capsys, tmp_path):
    assert cli.main(["install-skill", "--dir", str(tmp_path / "skills")]) == 0
    skill = tmp_path / "skills" / "agent-context" / "SKILL.md"
    assert capsys.readouterr().out.strip() == str(skill)
    text = skill.read_text()
    assert text.startswith("---\nname: agent-context\ndescription: ")
    assert "## Trust" in text and "agent-context list_spaces" in text


def test_skill_commands_match_server_tools():
    """Every command the skill shows must exist, so the docs can't drift from the tools."""
    import asyncio
    import re
    tools = {t.name for t in asyncio.run(server.create_server(server.db_path()).list_tools())}
    shown = set(re.findall(r"^agent-context (\w+)", cli.skill_text(), re.M))
    assert shown and shown <= tools
