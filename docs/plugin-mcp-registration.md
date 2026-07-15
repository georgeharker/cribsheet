# Plugin MCP registration — standalone vs combiner

How the cribsheet plugin decides whether to register the crib MCP server, so one
plugin serves both deployments: **standalone** (the plugin wires up crib itself)
and **combiner** (an `mcp-combiner` proxy already serves crib, so the plugin must
not register it a second time).

Status: design. Supersedes the two-plugin split (`cribsheet` +
`cribsheet-instructions`), which encoded the choice as *which plugin you install*.

## 1. The problem

crib is reachable two ways:

- **standalone** — the plugin registers `http://127.0.0.1:7732/mcp` and its
  SessionStart hook keeps one warm `crib` there via sharedserver.
- **combiner** — `mcp-combiner` proxies crib (and svg-mcp, jupyter, …) through a
  single endpoint; crib's tools arrive namespaced (`cribsheet_note_apropos`, …).

If a combiner user also installs the registering plugin, crib's whole tool surface
is mounted **twice** — duplicated tools, duplicated tokens, ambiguity about which
to call. Today we avoid that by shipping two near-identical plugins and pushing the
choice onto the installer. That does not scale: every backend behind the combiner
(svg-mcp, jupyter, …) has the same problem, so the split would have to be
duplicated per backend, and picking wrong fails silently.

Goal: **one plugin per backend**, with the deployment chosen by the environment
rather than by which artifact you install.

## 2. Constraints (measured, not assumed)

These were established empirically on Claude Code **v2.1.210**. They are the
reason the obvious designs don't work; record them so they aren't re-tried.

### 2.1 There is no `disable` for an MCP entry

`plugin.json`'s `mcpServers` and a plugin-shipped `.mcp.json` have **no**
`enabled` / `disabled` / `when` field. An entry that exists is an entry that
registers.

### 2.2 Env expansion can choose a URL, never remove a server

Only `${VAR}` and `${VAR:-default}` are supported (no `${VAR:+alt}`, no nesting),
and a plugin-shipped `.mcp.json` **does** expand them (proven: the mcp-combiner
plugin ships `${MCP_COMPANION_COMBINER_URL:-http://127.0.0.1:9741/mcp}`, that var
is unset, and it connects on the default).

Claude Code's `:-` is **not** bash's. Measured, with
`url: "${POLARITY_TEST:-http://127.0.0.1:7732/mcp}"`:

| `POLARITY_TEST` | result |
| --- | --- |
| unset | ✔ Connected (default used) |
| set, empty | ✘ **Failed to connect** |
| set to a URL | ✔ Connected (override) |

So set-but-empty keeps the empty value (bash would fall back to the default) — but
an env-emptied URL is a **red failed-server row**, not the quiet "not configured"
placeholder the docs describe for an empty `url`. **You cannot switch a server off
from env via `.mcp.json`.**

### 2.3 Hooks cannot change the current session's MCP set

The server set is resolved at session startup; hooks have no API to add, remove,
or suppress servers. A hook's config write therefore lands for the **next**
session.

### 2.4 No plugin can configure another plugin

A plugin cannot set env for another plugin's manifest expansion — expansion reads
the environment Claude Code was *launched with*. So "the combiner plugin turns off
the backend plugins" is not a load-order problem to solve; it is impossible. The
switch must be set **before Claude starts** — `zshenv` (§3.7), which every shell and
everything it spawns inherits.

### 2.5 Writing MCP config mid-session forces a reload

Observed: adding/removing a user-scope server while a session was live triggered an
MCP config reload that **dropped a connected combiner's 173 tools** for that
session. Config writes are not free, and are not silent.

## 3. Design

**One plugin. No `mcpServers` in the manifest; no shipped `.mcp.json`.**
Registration is performed by the SessionStart hook, which branches on a single
environment variable — because shell can express a conditional and manifest
expansion (§2.2) cannot.

```sh
# hooks/session-start.sh  (sketch)
if combiner_serves cribsheet; then          # §3.1
  # combiner is the MCP: ensure we are NOT registered
  claude mcp get cribsheet >/dev/null 2>&1 && \
    claude mcp remove cribsheet --scope user
else
  # standalone: ensure we ARE registered, and warm the backend
  claude mcp get cribsheet >/dev/null 2>&1 || \
    claude mcp add --transport http cribsheet http://127.0.0.1:7732/mcp --scope user
  sharedserver use cribsheet --pid "$PPID" --grace-period 1h -- \
    crib --mcp --http --host 127.0.0.1 --port 7732 >/dev/null 2>&1 || true
fi
```

### 3.1 The switch — global, with a per-backend override

A single global boolean is too blunt: the combiner rarely proxies *everything*, so
a backend must be able to disagree with the global. Two variables, per-backend wins:

| variable | scope | meaning |
| --- | --- | --- |
| `MCP_COMBINER` | all backends | is a combiner serving my MCPs? |
| `MCP_COMBINER_SERVES_<NAME>` | one backend | override for that backend only |

Resolution (`<NAME>` upper-cased, e.g. `MCP_COMBINER_SERVES_CRIBSHEET`):

```sh
# true  -> the combiner serves it -> DO NOT register (remove)
# false -> we serve it            -> register + warm the backend
combiner_serves() {                     # $1 = backend name, e.g. cribsheet
  local name per per_set
  name=$(printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_')
  eval "per=\${MCP_COMBINER_SERVES_$name-}"
  eval "per_set=\${MCP_COMBINER_SERVES_$name+set}"
  [ -n "$per_set" ] && { _truthy "$per"; return; }   # per-backend wins
  _truthy "${MCP_COMBINER-}"                          # else the global
}
_truthy() { case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
              ''|0|false|no|off) return 1 ;; *) return 0 ;; esac; }
```

**Values, not presence.** `_truthy` is deliberately value-based: presence-based
tests make `MCP_COMBINER=0` mean *"combiner on"*, and the per-backend override is
useless unless `0` can say *"no — force standalone here"*. Unset and empty both
read as false.

### 3.2 Polarity — default standalone

| `MCP_COMBINER` | `MCP_COMBINER_SERVES_CRIBSHEET` | crib mode |
| --- | --- | --- |
| unset | unset | **standalone** (default) |
| `1` | unset | combiner |
| `1` | `0` | **standalone** — combiner proxies the others, not crib |
| unset | `1` | combiner — only crib is proxied |

Unset/unset is the friendly default: a fresh marketplace install works with no
configuration. Combiner users opt out with one line in `zshenv`:

```sh
export MCP_COMBINER=1
```

This retires `CRIB_EXTERNAL`, which only ever gated *starting the backend* and
could not gate *registration* — the exact gap that made one plugin impossible.

### 3.3 Naming — why not `MCP_COMBINER_<NAME>`

`MCP_COMBINER_*` is **already the combiner's own settings namespace**
(`MCP_COMBINER_CONFIG`, `MCP_COMBINER_PORT`, `MCP_COMBINER_TOKEN_KEY`,
`MCP_COMBINER_HEALTH_INTERVAL`, `MCP_COMBINER_SCHEMA_FIXES`, …). A bare
`MCP_COMBINER_CRIBSHEET` is indistinguishable from a combiner *setting* — a
backend named `config` or `port` would collide outright, and a reader cannot tell
which system owns the variable. The `SERVES_` infix keeps the two apart and states
the question being asked.

### 3.4 The flip is bidirectional — both branches must mutate

Neither branch may be a no-op: the combiner branch actively *removes*, the
standalone branch actively *adds*. The hook is a **convergence step**, not a one-way
disable — every start it drives the registry to match the env, in whichever
direction it currently disagrees.

| current registry | env says | hook does | result |
| --- | --- | --- | --- |
| absent | standalone (unset) | `add` + warm | registered |
| present | standalone (unset) | nothing (guard hits) | registered — **no write** |
| present | combiner (set) | `remove` | unregistered |
| absent | combiner (set) | nothing (guard hits) | unregistered — **no write** |

Setting the env flips the machine to combiner; **unsetting it flips back** to
standalone. No manual `claude mcp` step either way. Each flip costs one session
(§3.5) and exactly one write; both steady-state rows write nothing, which is what
keeps §2.5 from biting on every session start.

Because the standalone branch **re-adds**, a manual `claude mcp remove` while the
env is unset will be undone next session. The env is the source of truth, not the
registry.

### 3.5 Idempotency is load-bearing

The guards are **not** an optimisation. Per §2.5 a write forces an MCP reload that
can disconnect live servers; an unguarded hook would inflict that on **every session
start**. Steady state must perform **zero writes**. Only a first run or a genuine
mode flip may write.

**The check must be cheap too.** Measured: `claude mcp get` costs **~1.7s**, against
**~35ms** to read the config JSON directly — and the check runs on every session
start, where it is pure overhead. So the hook reads `$CLAUDE_CONFIG_DIR/.claude.json`
(falling back to `~/.claude.json`) for the *check*, while every *mutation* still goes
through the supported `claude mcp add|remove`.

That read couples us to a config path we do not own, so it fails safe rather than
silently: it exits 2 for "can't tell" (path moved, unparseable, no python) and the
hook falls back to the slow-but-authoritative `claude mcp get`. Guessing "absent"
instead would re-add and reload MCP on **every** session — precisely the failure the
guard exists to prevent. Measured steady-state cost of the whole hook: **~45ms**.

### 3.6 Timing

Per §2.3, treat a write as applying to the **next** session. A flip (or first
install) costs one session. Do not depend on the reload in §2.5 landing the change
sooner — that is the same mechanism that drops other servers.

### 3.7 A global toggle, not a per-session one

**A design constraint, not an incidental property.** The switch says *how this
machine gets its MCPs*, and is set once, machine-wide:

```sh
# ~/.zshenv — set once; every shell, and everything it spawns, inherits it
export MCP_COMBINER=1
```

`zshenv` satisfies §2.4 (set before Claude starts) for every path — interactive
shells, neovim, and the ACP/codecompanion agents neovim spawns all inherit it.
**Nothing needs to inject it per session.**

**Do not vary it per session.** The registry it drives (`claude mcp --scope user`)
is global, so two concurrent sessions disagreeing about the mode would thrash, each
hook converging the *shared* registry against its own env and undoing the other at
every start. The switch and the state it controls are both global; keeping them at
the same scope is what makes §3.4's convergence sound.

A machine is therefore in exactly one mode. `MCP_COMBINER_SERVES_CRIBSHEET` is
still a global statement about crib — not a per-session escape hatch.

## 4. Risks accepted

- **Global toggle only** (§3.7) — machine-wide by design; varying it per session is
  unsupported and would thrash the shared user-scope registry. Project scope would
  allow per-repo modes but requires approval and pollutes repos, so it is not offered.
- **A plugin writing global MCP config** is unusual, surprising behaviour. It must
  be documented prominently in the plugin README.
- **Uninstall leaves a stray entry.** Removing the plugin does not unregister
  `cribsheet`; a SessionEnd/uninstall path should clean up, or the README must say so.
- **One-session lag** on first install and on every mode flip (§3.6).

## 5. Generalising to the family

Every backend behind the combiner has this problem, so the pattern is the
convention, not a crib special case:

- One plugin per backend; **no backend plugin declares `mcpServers`**.
- Each plugin's SessionStart hook manages **only its own** entry, resolving the
  switch with the shared `combiner_serves <name>` logic (§3.1) — global
  `MCP_COMBINER`, overridden per backend by `MCP_COMBINER_SERVES_<NAME>`.
- The switch is set once, machine-wide, outside the session — `zshenv` (§3.7).
  Interactive shells, neovim, and the ACP/codecompanion agents it spawns all
  inherit it; nothing injects it per session.

A partial-proxy setup is then expressible, which is the common real case:

```sh
export MCP_COMBINER=1                    # combiner serves my MCPs…
export MCP_COMBINER_SERVES_CRIBSHEET=0   # …except crib, which I run standalone
```

`combiner_serves` should be **copied, not shared**: plugins cannot depend on each
other (§2.4), so a shared helper would be a cross-plugin dependency we cannot
express. It is ~10 lines; duplicate it per backend and keep the semantics
identical. (For cribsheet specifically it could instead live in the `crib` CLI.)

The combiner remains the single source of truth for *what it proxies* (its
`mcpservers.json`), and nothing keeps the env in sync with it — a backend added to
the combiner but not exported here silently double-registers. A later refinement,
not required by this design, is a `mcp-combiner env-disable` that emits the whole
switch set directly from that config, so the two cannot drift.

## 6. Migration

1. Fold `commands/` + `instructions.txt` + hooks into the single `cribsheet` plugin;
   drop its `mcpServers` block.
2. Retire `cribsheet-instructions` from the marketplace. **Done** — the unified plugin
   serves both modes, so the instructions-only variant has no remaining role.
3. Combiner machines: `export MCP_COMBINER=1` in `zshenv` **before** the switch, so
   the first session after upgrade does not register crib alongside the combiner.
4. Standalone machines: nothing to do.

Note the plugin command stays namespaced (`/cribsheet:crib`) — plugin commands are
always `/<plugin>:<command>`. A bare `/crib` is only available from a user-level
`$CLAUDE_CONFIG_DIR/commands/crib.md`, which is independent of this design.
