---
name: agent-context
description: Persistent memory across sessions, shared with other agents on this machine. Use to remember a decision or finding with its source, to recall what was saved earlier for a project, or to record relationships (dependencies, ownership) and derive implied ones with rules.
---

# Agent context

A local database of sourced notes, facts and rules, reached through the `agent-context` command. Every command prints JSON; errors go to stderr with exit status 1.

## Steps

1. **Pick the space.** Run `agent-context list_spaces`; reuse the space that matches the current project or user, or name a new one after it.
2. **Recall before answering.** Search existing context with `search_notes`, browse with `list_notes`, and check relationships with `list_facts` or `infer`. Done when you have either found the relevant records or tried two different keyword sets.
3. **Remember what will matter later.** Store each durable decision or finding with a stable, descriptive ID and its original source (file path, URL, meeting). Reuse an ID to correct a record.
4. **Report with provenance.** Cite record IDs and sources, and say which facts were asserted and which were derived.

## Commands

Each command takes `--name value` flags matching the fields below; `agent-context <command> --help` lists them. JSON fields (`metadata`, `head`, `body`) take JSON text, and `--content -` reads the note from stdin.

```sh
agent-context list_spaces
agent-context search_notes --space my-project --query "database choice"
agent-context list_notes --space my-project --after last-id-seen
agent-context get_note --space my-project --id decision-db
agent-context put_note --space my-project --id decision-db --title "Database" \
  --source "docs/adr/0003.md" --content - < note.md
agent-context put_fact --space my-project --id checkout-payments \
  --subject service:checkout --predicate depends_on --object service:payments --source repo:checkout/config
agent-context put_rule --space my-project --id transitive-deps --source policy:deps \
  --head '["?x","depends_on","?z"]' --body '[["?x","depends_on","?y"],["?y","depends_on","?z"]]'
agent-context infer --space my-project --subject service:checkout
agent-context delete_record --space my-project --kind note --id decision-db
```

The database is `$CONTEXT_DB` if set, otherwise `agent-context/context.db` under `$XDG_DATA_HOME` (default `~/.local/share`).
