---
description: Consult crib first — recall memory + the code index for a topic before answering
argument-hint: "<topic or question>"
---
The "consult crib first" habit as one command. Given the topic in `$ARGUMENTS`,
pull what crib already knows before reasoning from scratch.

If `$ARGUMENTS` is empty, ask the user what to recall and stop — don't run the
commands below with an empty query.

Otherwise run exactly these (they attach to the warm, sharedserver-managed crib
daemon; no cold start):

Memory — semantic recall, full matched sections:

!`"${CLAUDE_PLUGIN_ROOT}/bin/crib" note apropos "$ARGUMENTS"`

Code index — symbols by concept or name (skip if the topic is clearly not about
code; a "project not indexed" reply just means there's nothing to add here):

!`"${CLAUDE_PLUGIN_ROOT}/bin/crib" code lookup "$ARGUMENTS"`

Then summarize what crib already knows about "$ARGUMENTS": lead with the notes'
answer, fold in any relevant code symbols, and cite note/symbol names so the
user can `crib note read` / `crib code dossier` to go deeper. If nothing
relevant came back from either, say so plainly rather than guessing — that's a
signal the knowledge isn't captured yet (consider `crib note store`).
