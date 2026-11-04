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
