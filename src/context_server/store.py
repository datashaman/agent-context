import json
import re
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def text(value: str, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be nonblank text of at most {maximum} characters")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ValueError(f"{name} contains control characters")
    return value


def expiry(value: str | None) -> str | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("expires_at requires a timezone")
    return dt.astimezone(timezone.utc).isoformat()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def limit_value(value: int) -> int:
    if not 1 <= value <= 100:
        raise ValueError("limit must be between 1 and 100")
    return value


def validate_rule(head: list[str], body: list[list[str]]) -> dict:
    if not isinstance(body, list) or not 1 <= len(body) <= 3:
        raise ValueError("body needs 1 to 3 triple patterns")
    for triple in [head, *body]:
        if not isinstance(triple, list) or len(triple) != 3:
            raise ValueError("each pattern must be [subject, predicate, object]")
        for term in triple:
            text(term, "rule term")
            if term.startswith("?") and not re.fullmatch(r"\?[a-zA-Z][a-zA-Z0-9_]*", term):
                raise ValueError("variables must look like ?x")
    variables = {t for triple in body for t in triple if t.startswith("?")}
    if len(variables) > 3:
        raise ValueError("at most 3 distinct variables per rule")
    if any(t.startswith("?") and t not in variables for t in head):
        raise ValueError("all head variables must occur in the body")
    return {"head": head, "body": body}


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS notes (
                    space TEXT, id TEXT, title TEXT NOT NULL, content TEXT NOT NULL,
                    source TEXT NOT NULL, metadata TEXT NOT NULL,
                    updated_at TEXT NOT NULL, expires_at TEXT,
                    PRIMARY KEY(space, id)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
                    title, content, content=notes, content_rowid=rowid
                );
                CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
                    INSERT INTO notes_fts(rowid,title,content) VALUES(new.rowid,new.title,new.content);
                END;
                CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
                    INSERT INTO notes_fts(notes_fts,rowid,title,content)
                    VALUES('delete',old.rowid,old.title,old.content);
                END;
                CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
                    INSERT INTO notes_fts(notes_fts,rowid,title,content)
                    VALUES('delete',old.rowid,old.title,old.content);
                    INSERT INTO notes_fts(rowid,title,content) VALUES(new.rowid,new.title,new.content);
                END;
                CREATE TABLE IF NOT EXISTS facts (
                    space TEXT, id TEXT, subject TEXT NOT NULL, predicate TEXT NOT NULL,
                    object TEXT NOT NULL, source TEXT NOT NULL,
                    updated_at TEXT NOT NULL, expires_at TEXT,
                    PRIMARY KEY(space, id)
                );
                CREATE INDEX IF NOT EXISTS facts_subject ON facts(space,subject);
                CREATE INDEX IF NOT EXISTS facts_object ON facts(space,object);
                CREATE TABLE IF NOT EXISTS rules (
                    space TEXT, id TEXT, definition TEXT NOT NULL, source TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(space,id)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def put_note(self, space, id, title, content, source, metadata=None, expires_at=None):
        for name, value in [("space", space), ("id", id), ("title", title), ("source", source)]:
            text(value, name)
        text(content, "content", 100_000)
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        metadata_json = json.dumps(metadata or {}, allow_nan=False)
        text(metadata_json, "metadata", 10_000)
        with self.connect() as db:
            db.execute("""INSERT INTO notes VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(space,id) DO UPDATE SET title=excluded.title,
                content=excluded.content, source=excluded.source, metadata=excluded.metadata,
                updated_at=excluded.updated_at, expires_at=excluded.expires_at""",
                (space, id, title, content, source, metadata_json, now(), expiry(expires_at)))
        return {"id": id, "space": space}

    @staticmethod
    def note(row):
        item = dict(row)
        item["metadata"] = json.loads(item["metadata"])
        return item

    def get_note(self, space, id):
        text(space, "space")
        text(id, "id")
        with self.connect() as db:
            row = db.execute("SELECT * FROM notes WHERE space=? AND id=? AND (expires_at IS NULL OR expires_at>?)",
                             (space, id, now())).fetchone()
        return self.note(row) if row else None

    def search_notes(self, space, query, limit=10):
        text(space, "space")
        limit_value(limit)
        text(query, "query", 2000)
        # Treat user text as literal tokens, never as executable FTS query syntax.
        tokens = re.findall(r"\w+", query, re.UNICODE)
        if not tokens:
            return []
        match = " OR ".join('"' + t + '"' for t in tokens[:32])
        with self.connect() as db:
            rows = db.execute("""SELECT n.* FROM notes_fts JOIN notes n ON n.rowid=notes_fts.rowid
                WHERE notes_fts MATCH ? AND n.space=? AND (n.expires_at IS NULL OR n.expires_at>?)
                ORDER BY bm25(notes_fts),n.id LIMIT ?""", (match, space, now(), limit)).fetchall()
        return [self.note(row) for row in rows]

    def put_fact(self, space, id, subject, predicate, object, source, expires_at=None):
        for name, value in [("space", space), ("id", id), ("subject", subject),
                            ("predicate", predicate), ("object", object), ("source", source)]:
            text(value, name)
        with self.connect() as db:
            db.execute("""INSERT INTO facts VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(space,id) DO UPDATE SET subject=excluded.subject,
                predicate=excluded.predicate, object=excluded.object, source=excluded.source,
                updated_at=excluded.updated_at, expires_at=excluded.expires_at""",
                (space, id, subject, predicate, object, source, now(), expiry(expires_at)))
        return {"id": id, "space": space}

    def list_facts(self, space, subject=None, predicate=None, object=None, limit=50):
        text(space, "space")
        limit_value(limit)
        clauses = ["space=?", "(expires_at IS NULL OR expires_at>?)"]
        args = [space, now()]
        for name, value in [("subject", subject), ("predicate", predicate), ("object", object)]:
            if value is not None:
                text(value, name)
                clauses.append(f"{name}=?")
                args.append(value)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM facts WHERE " + " AND ".join(clauses)
                              + " ORDER BY id LIMIT ?", [*args, limit]).fetchall()
        return [dict(row) for row in rows]

    def put_rule(self, space, id, head, body, source):
        for name, value in [("space", space), ("id", id), ("source", source)]:
            text(value, name)
        rule = validate_rule(head, body)
        with self.connect() as db:
            db.execute("""INSERT INTO rules VALUES(?,?,?,?,?) ON CONFLICT(space,id)
                DO UPDATE SET definition=excluded.definition,source=excluded.source,
                updated_at=excluded.updated_at""", (space, id, json.dumps(rule), source, now()))
        return {"id": id, "space": space}

    def list_rules(self, space):
        text(space, "space")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM rules WHERE space=? ORDER BY id", (space,)).fetchall()
        return [{**dict(row), "definition": json.loads(row["definition"])} for row in rows]

    def delete(self, space, kind, id):
        if kind not in {"note", "fact", "rule"}:
            raise ValueError("kind must be note, fact, or rule")
        text(space, "space")
        text(id, "id")
        with self.connect() as db:
            count = db.execute(f"DELETE FROM {kind}s WHERE space=? AND id=?", (space, id)).rowcount
        return {"deleted": bool(count), "id": id, "space": space, "kind": kind}

    def infer(self, space, subject=None, predicate=None, object=None, limit=50):
        text(space, "space")
        limit_value(limit)
        for name, value in [("subject", subject), ("predicate", predicate), ("object", object)]:
            if value is not None:
                text(value, name)
        # One read transaction makes the facts/rules a consistent snapshot.
        with self.connect() as db:
            db.execute("BEGIN")
            snapshot_at = now()
            facts = [dict(row) for row in db.execute("""SELECT * FROM facts WHERE space=?
                AND (expires_at IS NULL OR expires_at>?) ORDER BY id LIMIT 2001""", (space, snapshot_at))]
            rules = [dict(row) for row in db.execute("SELECT * FROM rules WHERE space=? ORDER BY id LIMIT 33", (space,))]
        if len(facts) > 2000 or len(rules) > 32:
            raise ValueError("inference supports at most 2000 active facts and 32 rules per space")
        try:
            result = subprocess.run([sys.executable, "-m", "context_server.reasoner"],
                input=json.dumps({"facts": facts, "rules": rules}), text=True,
                capture_output=True, timeout=8, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("inference exceeded 8 seconds; reduce facts or rules") from exc
        if result.returncode:
            raise ValueError("inference failed: " + result.stderr[-1000:])
        triples = json.loads(result.stdout)
        selected = [t for t in triples if all(v is None or v == t[i]
                    for i, v in enumerate((subject, predicate, object)))]
        asserted = {}
        for fact in facts:
            key = (fact["subject"], fact["predicate"], fact["object"])
            asserted.setdefault(key, []).append({"id": fact["id"], "source": fact["source"]})
        return {"space": space, "snapshot_at": snapshot_at, "total_matches": len(selected),
                "truncated": len(selected) > limit,
                "facts": [{"subject": t[0], "predicate": t[1], "object": t[2],
                    "derived": tuple(t) not in asserted, "assertions": asserted.get(tuple(t), [])}
                    for t in selected[:limit]],
                "rules": [{"id": r["id"], "source": r["source"],
                           "definition": json.loads(r["definition"])} for r in rules],
                "notice": "Derived facts follow from this snapshot and rules, not independent evidence. "
                          "Rules listed are the full ruleset, not a per-result proof. Missing facts mean unknown."}
