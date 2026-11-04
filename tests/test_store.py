import sqlite3
import subprocess

import pytest

from context_server.store import Store, validate_rule


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "context.db")


def test_notes_persist_update_expire_and_isolate(store):
    store.put_note("a", "decision", "Database", "Use SQLite", "meeting:1", {"owner": "Sam"})
    store.put_note("b", "decision", "Database", "Use Postgres", "meeting:2")
    assert Store(store.path).get_note("a", "decision")["metadata"] == {"owner": "Sam"}
    assert len(store.search_notes("a", "SQLite")) == 1
    assert store.search_notes("b", "SQLite") == []
    store.put_note("a", "decision", "Cache", "Use Redis", "meeting:3")
    assert store.search_notes("a", "SQLite") == []
    assert len(store.search_notes("a", "Redis")) == 1
    store.put_note("a", "expired", "Redis", "Old Redis choice", "meeting:0", expires_at="2000-01-01T00:00:00Z")
    assert store.get_note("a", "expired") is None
    assert len(store.search_notes("a", "Redis")) == 1
    store.delete("a", "note", "decision")
    assert store.search_notes("a", "Redis") == []
    assert store.get_note("b", "decision") is not None


def ids(records):
    return [r["id"] for r in records]


def walk(page):
    """Follow the after cursor until a short page; return every id seen."""
    seen, after = [], None
    while True:
        batch = ids(page(after))
        seen += batch
        if len(batch) < 100:
            return seen
        after = batch[-1]


def test_list_spaces_counts_active_records(store):
    assert store.list_spaces() == []
    store.put_note("zeta", "n1", "t", "c", "s")
    store.put_note("zeta", "n2", "t", "c", "s")
    store.put_fact("alpha", "f1", "a", "p", "b", "s")
    transitive(store, "alpha")
    store.put_note("gone", "old", "t", "c", "s", expires_at="2000-01-01T00:00:00Z")
    store.put_fact("zeta", "old", "a", "p", "b", "s", expires_at="2000-01-01T00:00:00Z")
    assert store.list_spaces() == [  # name order; expired records neither counted nor listed
        {"space": "alpha", "notes": 0, "facts": 1, "rules": 1},
        {"space": "zeta", "notes": 2, "facts": 0, "rules": 0},
    ]
    store.delete("alpha", "fact", "f1")
    store.delete("alpha", "rule", "transitive")
    assert [s["space"] for s in store.list_spaces()] == ["zeta"]


def test_list_notes_browses_without_keyword(store):
    store.put_note("a", "zeta", "Z title", "z body", "s", {"tag": "x"})
    store.put_note("a", "alpha", "A title", "a body", "s")
    store.put_note("a", "old", "Old", "gone", "s", expires_at="2000-01-01T00:00:00Z")
    store.put_note("b", "beta", "B", "b", "s")
    listed = store.list_notes("a")
    assert ids(listed) == ["alpha", "zeta"]  # ID order, expired hidden, space isolated
    assert set(listed[0]) == {"id", "title", "source", "updated_at", "expires_at"}
    assert ids(store.list_notes("a", limit=1)) == ["alpha"]
    assert ids(store.list_notes("a", after="alpha")) == ["zeta"]
    assert store.list_notes("a", after="zeta") == []
    assert store.list_notes("empty") == []
    store.delete("a", "note", "alpha")
    assert ids(store.list_notes("a")) == ["zeta"]


def test_cursor_walks_every_record_past_page_cap(store):
    for i in range(250):
        store.put_note("a", f"n{i:03}", "t", "c", "s")
        store.put_fact("a", f"f{i:03}", "x" if i % 2 else "y", "p", "o", "s")
    assert walk(lambda after: store.list_notes("a", after=after, limit=100)) == [f"n{i:03}" for i in range(250)]
    assert walk(lambda after: store.list_facts("a", after=after, limit=100)) == [f"f{i:03}" for i in range(250)]
    odd = walk(lambda after: store.list_facts("a", subject="x", after=after, limit=100))
    assert odd == [f"f{i:03}" for i in range(1, 250, 2)]


def test_list_facts_paging_hides_expired_and_other_spaces(store):
    store.put_fact("a", "f1", "x", "p", "o", "s")
    store.put_fact("a", "f2", "x", "p", "o", "s", expires_at="2000-01-01T00:00:00Z")
    store.put_fact("a", "f3", "x", "p", "o", "s")
    store.put_fact("b", "f2b", "x", "p", "o", "s")
    assert ids(store.list_facts("a", after="f1")) == ["f3"]
    assert store.list_facts("a", after="f3") == []


@pytest.mark.parametrize("call", [
    lambda s: s.list_notes("a", limit=0),
    lambda s: s.list_notes("a", limit=101),
    lambda s: s.list_notes("a", after=""),
    lambda s: s.list_notes("a", after="x\x00"),
    lambda s: s.list_notes(""),
    lambda s: s.list_facts("a", after="   "),
    lambda s: s.list_facts("a", after="x" * 513),
])
def test_listing_rejects_bad_arguments(store, call):
    with pytest.raises(ValueError):
        call(store)


def transitive(store, space="a"):
    store.put_rule(space, "transitive", ["?x", "depends", "?z"],
                   [["?x", "depends", "?y"], ["?y", "depends", "?z"]], "policy:dependencies")


def test_inference_retraction_sources_cycles_and_isolation(store):
    store.put_fact("a", "f1", "checkout", "depends", "payments", "repo:checkout")
    store.put_fact("a", "f2", "payments", "depends", "identity", "repo:payments")
    store.put_fact("b", "f3", "identity", "depends", "secret", "private")
    transitive(store)
    result = store.infer("a", subject="checkout")
    assert result["total_matches"] == 2
    derived = next(f for f in result["facts"] if f["object"] == "identity")
    assert derived["derived"] is True and derived["assertions"] == []
    base = next(f for f in result["facts"] if f["object"] == "payments")
    assert base["assertions"] == [{"id": "f1", "source": "repo:checkout"}]
    assert result["rules"][0]["source"] == "policy:dependencies"
    store.put_fact("a", "cycle", "identity", "depends", "checkout", "repo:identity")
    assert store.infer("a")["total_matches"] == 9
    store.delete("a", "fact", "cycle")
    store.delete("a", "fact", "f2")
    assert store.infer("a", subject="checkout")["total_matches"] == 1


def test_rule_removal_and_fact_replacement(store):
    store.put_fact("a", "f1", "a", "depends", "b", "s")
    store.put_fact("a", "f2", "b", "depends", "c", "s")
    transitive(store)
    assert store.infer("a", object="c")["total_matches"] == 2
    store.put_fact("a", "f2", "b", "depends", "d", "s")
    assert store.infer("a", object="c")["total_matches"] == 0
    store.delete("a", "rule", "transitive")
    assert store.infer("a")["total_matches"] == 2


def test_expired_facts_and_duplicate_evidence(store):
    store.put_fact("a", "old", "a", "p", "b", "s", "2000-01-01T00:00:00+00:00")
    assert store.list_facts("a") == []
    assert store.infer("a")["facts"] == []
    for id in ("one", "two"):
        store.put_fact("a", id, "a", "p", "b", "source:" + id)
    result = store.infer("a")
    assert len(result["facts"]) == 1
    assert len(result["facts"][0]["assertions"]) == 2


def test_untrusted_strings_are_data(store):
    hostile = 'x"). injected. % \\ " ; DROP TABLE facts;'
    store.put_fact("a", "f", hostile, "p", "🍎", "s")
    assert store.infer("a")["facts"][0]["subject"] == hostile
    store.put_note("a", "n", "title", "ordinary document", "s")
    assert isinstance(store.search_notes("a", '" OR NOT * : ;'), list)
    assert store.get_note("a", "n") is not None


@pytest.mark.parametrize("head,body", [
    (["?x", "p", "?y"], [["?x", "p", "b"]]),
    (["a", "b"], [["a", "b", "c"]]),
    (["a", "b", "c"], []),
    (["a", "b", "c"], [["?bad-name", "b", "c"]]),
    (["?a", "p", "?b"], [["?a", "?b", "?c"], ["?c", "p", "?d"]]),
])
def test_invalid_rules(head, body):
    with pytest.raises(ValueError):
        validate_rule(head, body)


def test_validation_and_truncation(store):
    with pytest.raises(ValueError):
        store.put_note("", "a", "b", "c", "s")
    with pytest.raises(ValueError):
        store.put_fact("a", "id", "a", "p", "b", "s", "2027-01-01")
    with pytest.raises(ValueError):
        store.list_facts("a", limit=0)
    with pytest.raises(ValueError):
        store.delete("a", "facts; DROP TABLE notes", "id")
    store.put_fact("a", "1", "a", "p", "b", "s")
    store.put_fact("a", "2", "b", "p", "c", "s")
    result = store.infer("a", limit=1)
    assert result["truncated"] and result["total_matches"] == 2


def test_note_input_checks_and_lookups(store):
    with pytest.raises(ValueError, match="metadata must be an object"):
        store.put_note("a", "n", "t", "c", "s", metadata=["not", "a", "dict"])
    with pytest.raises(ValueError, match="metadata"):
        store.put_note("a", "n", "t", "c", "s", metadata={"big": "x" * 10_000})
    with pytest.raises(ValueError):
        store.put_note("a", "n", "t", "c", "s", metadata={"bad": float("nan")})
    with pytest.raises(ValueError, match="content"):
        store.put_note("a", "n", "t", "x" * 100_001, "s")
    with pytest.raises(ValueError, match="control"):
        store.put_note("a", "n", "t\x07", "c", "s")
    with pytest.raises(ValueError, match="timezone"):
        store.put_note("a", "n", "t", "c", "s", expires_at="2099-01-01T00:00:00")
    store.put_note("a", "n", "t", "line\nbreak\ttab", "s", expires_at="2099-01-01T02:00:00+02:00")
    assert store.get_note("a", "n")["expires_at"] == "2099-01-01T00:00:00+00:00"  # normalised to UTC
    store.put_note("a", "n", "t", "c", "s")
    assert store.get_note("a", "n")["expires_at"] is None  # replacing without expiry clears it
    assert store.get_note("a", "missing") is None
    assert store.search_notes("a", "!!! ???") == []  # no word tokens


def test_search_ranks_and_limits(store):
    store.put_note("a", "weak", "misc", "sqlite once", "s")
    store.put_note("a", "strong", "sqlite", "sqlite sqlite sqlite", "s")
    assert [n["id"] for n in store.search_notes("a", "SQLite")] == ["strong", "weak"]
    assert len(store.search_notes("a", "sqlite", limit=1)) == 1


def test_rules_listing_replacement_and_delete_result(store):
    assert store.list_rules("a") == []
    transitive(store)
    store.put_rule("a", "transitive", ["?x", "linked", "?y"], [["?x", "depends", "?y"]], "v2")
    [rule] = store.list_rules("a")
    assert rule["source"] == "v2" and rule["definition"]["head"] == ["?x", "linked", "?y"]
    assert store.delete("a", "rule", "transitive")["deleted"] is True
    assert store.delete("a", "rule", "transitive")["deleted"] is False  # idempotent


def test_fact_filters_combine(store):
    store.put_fact("a", "1", "x", "p", "y", "s")
    store.put_fact("a", "2", "x", "q", "y", "s")
    store.put_fact("a", "3", "z", "p", "y", "s")
    assert ids(store.list_facts("a", subject="x", predicate="p", object="y")) == ["1"]
    assert ids(store.list_facts("a", object="y", limit=2)) == ["1", "2"]
    assert store.infer("a", predicate="q")["facts"][0]["subject"] == "x"
    with pytest.raises(ValueError, match="subject"):
        store.infer("a", subject="")


def test_infer_space_size_limits(store, monkeypatch):
    with sqlite3.connect(store.path) as db:
        db.executemany("INSERT INTO facts VALUES('big',?,?,'p','o','s','t',NULL)",
                       [(str(i), str(i)) for i in range(2001)])
        db.executemany("INSERT INTO rules VALUES('many',?,?,'s','t')",
                       [(str(i), '{"head":["a","b","c"],"body":[["a","b","c"]]}') for i in range(33)])
    with pytest.raises(ValueError, match="2000 active facts"):
        store.infer("big")
    with pytest.raises(ValueError, match="32 rules"):
        store.infer("many")


def test_reasoner_crash_is_reported(store, monkeypatch):
    failed = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: failed)
    with pytest.raises(ValueError, match="inference failed: boom"):
        store.infer("a")


def test_new_database_is_private_and_reopens(tmp_path):
    path = tmp_path / "nested" / "dir" / "context.db"
    Store(path).put_note("a", "n", "t", "c", "s")
    assert path.stat().st_mode & 0o777 == 0o600
    assert Store(path).get_note("a", "n")["content"] == "c"  # schema creation is idempotent


def test_domain_limit_is_explicit(store):
    for i in range(129):
        store.put_fact("a", str(i), str(i), "p", "b", "s")
    with pytest.raises(ValueError, match="128"):
        store.infer("a")


def test_wal_and_fts_available(store):
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_timeout_is_reported_without_partial_results(store, monkeypatch):
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired("reasoner", 8)

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(ValueError, match="exceeded 8 seconds"):
        store.infer("a")


def test_grounding_budget_is_explicit(store):
    for i in range(101):
        store.put_fact("a", str(i), str(i), "depends", "target", "s")
    transitive(store)
    with pytest.raises(ValueError, match="grounding estimate"):
        store.infer("a")
