---
title: "cribsheet"
---

*Persistent memory for your AI — plain markdown on disk, semantically indexed.*

cribsheet gives your AI assistant a durable, searchable memory it keeps across
sessions and shares across every agent and tool you run — as plain markdown you
own, not a black box. It remembers **notes** (facts, conventions, gotchas, found by
meaning), **code** (a symbol index of your repos with concept search, a real
call graph, and durable *learnings* you pin to symbols), and **decisions and plans**
as a graph rather than prose — `grep` can't tell you what a change invalidates, but
edges can: change a decision and you're told which others just went stale, and a
plan item survives to tell a later session what's actionable.

Start with the **[overview & quick start](README.md)**, then dive in:

### Using cribsheet

| Document | What it covers |
|---|---|
| [User guide](docs/guide.md) | the six facets, the noun-verb interface, runnable workflows |
| [CLI ⇄ MCP reference](docs/surface.md) | every noun, every verb, one line each |
| [Where things live](docs/storage.md) | store types, who owns the bytes, which verbs may write them |
| [Sync across machines](docs/resume-on-new-machine.md) | share your memory over plain git |

### Under the hood

| Document | What it covers |
|---|---|
| [Design & why](DESIGN.md) | the architecture and the reasoning, end to end |
| [Implementation map](docs/implementation.md) | subsystem-by-subsystem, anchored to files and symbols |
| [Code symbol index](docs/code-symbol-index.md) | how the code↔note index is built, and the learnings model |
| [Retrieval & adoption](docs/retrieval-and-adoption.md) | retrieval quality, and why delivery makes a memory tool get used |
| [Knowledge capture](docs/knowledge-capture.md) | `distill` / `elaborate` / `summarize`, the generation layer |
