"""How a symbol is spelled, encoded, and matched — the one place that decides.

A symbol reaches the outside world in several spellings, and every one of them is a
CONVENTION that a writer and a reader have to agree on:

  * the reference (THE KEY)     `crib/app.py#Crib.code_graph`  (`symbol_ref`)
  * the name                    `crib.app.Crib.code_graph`, `a::b::C::d`  (`fqn`)
  * the edge ref                `helper [src/util.py]`, `helper [dep:src/util.py]`
  * the location inside it      `src/util.py` or `dep:src/util.py`
  * the cross-project handle    `dep:util.helper`
  * the on-disk basename        `ref_slug(symbol_ref)`

Each convention lives here exactly once, as a named function, so "where is this
decided" has one answer and a change to a spelling is a change to one file.

PURE LEAF, and both halves are load-bearing:

  * LEAF — imports nothing from `crib`. That is what lets every other module import
    it at the top instead of reaching in from inside a function to dodge a cycle.
    `codeindex` is not a leaf, so a shared rule parked there forces exactly that
    dodge on `codestore`, `refs`, `codequery` and `learnings` in turn.
  * PURE — no I/O, no state, no config, no clock. Values in, values out. So it is
    safe in the indexer's inner loop, testable with no fixture, and it will not
    grow a reason to import something and stop being a leaf.

The contract is narrower than "pure", because a pure leaf is exactly the module that
becomes a junk drawer: it is about how a symbol is SPELLED. Argument validation is
not spelling. Building a search field is not spelling. Anything that needs the
filesystem, an LSP or a store is not spelling — it stays with the plumbing and calls
in here for the last step (`_locate` resolves a URI against real paths, then asks
`encode_loc` how to write the answer down).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Sequence

# ── qualified names ───────────────────────────────────────────────────────────

_ANY_SEP = re.compile(r"::|\.")


def fqname_sep(lang: str) -> str:
    """The separator a qualified name uses for `lang` — the ONE place the writer of a
    qualified name and every reader of one agree, so a language added to either side
    cannot be missing from the other."""
    return "::" if lang == "rust" else "."


def segments(fq: str, lang: str) -> list[str]:
    """`fq` cut into segments IN ITS OWN LANGUAGE'S TERMS — never a normalized
    spelling, because normalizing would have to pick some character to mean
    "separator" and then could not tell it apart from one occurring inside a name.
    An unknown `lang` degrades to splitting on either separator, which is a floor
    rather than the rule."""
    if not fq:
        return []
    return fq.split(fqname_sep(lang)) if lang else _ANY_SEP.split(fq)


def tail(fq: str) -> str:
    """The bare last segment, splitting on either separator — for a caller that has
    no `lang` to hand (a describe row echoing what the source file showed)."""
    return _ANY_SEP.split(fq)[-1]


def _own_segments(fq: str, name: str, lang: str) -> list[str]:
    """`fq` cut into segments, with the entry's recorded `name` taken as the
    AUTHORITATIVE final one.

    A name may legally contain the separator (a zsh function called `git.push`), so
    re-deriving the tail by splitting would invent boundaries that were never there
    and let `push` match a symbol not called `push`. Rebuild from what the writer
    recorded instead."""
    seg = segments(fq, lang)
    if name and fq.endswith(name):
        head = fq[:-len(name)].rstrip(fqname_sep(lang) if lang else ".:")
        seg = (segments(head, lang) if head else []) + [name]
    return seg


def suffix_of(fq: str, partial: str) -> bool:
    """Whether `partial` is a trailing run of `fq`'s segments, on a separator
    boundary — the describe-row match, where only the two strings are in hand."""
    if not partial or not fq:
        return False
    return any(fq.endswith(sep + partial) for sep in (".", "::"))


# ── the local name, and what a symbol's own language calls its scope ───────────

# Lua binds modules to a local table var (`M.setup`); those aren't real qualifiers.
LUA_TABLE_VARS = {"M", "_M", "self", "Module", "mod"}


def local_name(raw: str, lang: str) -> str:
    """The bare symbol name — strip a Lua module-table prefix (`M.setup`→`setup`,
    `T:method`→`method`) that documentSymbol folds into the name, and reduce a Rust
    `impl` block's name to the TYPE it's for, so its methods qualify as `Type::method`
    (rust-analyzer names impl symbols `impl Type` / `impl Trait for Type`)."""
    if lang == "lua":
        return raw.replace(":", ".").split(".")[-1]
    if lang == "rust" and re.match(r"impl\b", raw):
        body = re.sub(r"^impl\s*<[^>]*>", "impl", raw)[4:].strip()  # drop impl-generics
        if " for " in body:                       # `Trait for Type` → the Type
            body = body.split(" for ")[-1].strip()
        base = re.sub(r"<.*$", "", body).strip().split("::")[-1]    # strip type args/path
        return base or raw
    return raw




# Whether a file's PATH forms part of the language's own qualified name, and from
# which root:
#   "module"  — the path IS the namespace, file included (python, lua: import/require)
#   "package" — the last DIRECTORY is the namespace; the filename is not (go)
#   "crate"   — namespace is crate-relative: everything after the last `src` (rust)
#   absent    — the path contributes NOTHING. Either the language declares its
#               namespace in the source (c++, ruby) or it has none (c, zsh, sh).
_PATH_NAMESPACE = {"python": "module", "lua": "module", "go": "package",
                   "rust": "crate"}
# FILE-SCOPED languages: the file IS the scope. Not "no scope" — a zsh symbol's
# bare name collides 32 times in one repo and 84 in another, while file + name
# collides zero times in both. What was wrong with `bin.sharedserver-watcher.main`
# was never "the path is the scope"; it was rendering a path AS A DOTTED NAMESPACE,
# which is false and unparseable (filenames carry hyphens, identifiers do not).
FILE_SCOPED = frozenset({"c", "zsh", "sh", "bash", "fish", "make"})
# Source roots that are build layout rather than namespace, stripped from the front.
_PATH_ROOTS = {"python": ("src",), "lua": ("lua",), "go": ("src",)}
# The file whose name is its DIRECTORY's namespace, not its own segment.
_INDEX_FILE = {"rust": "mod", "lua": "init", "python": "__init__"}




def _path_scope(lang: str, file: str) -> list[str]:
    """The part of a language's qualified name that comes from the file path."""
    mode = _PATH_NAMESPACE.get(lang)
    if not mode or not file:
        return []
    parts = list(Path(file).with_suffix("").parts)
    if mode == "crate":
        # crate-RELATIVE: the crate name lives in Cargo.toml, out of band, and adds
        # nothing `path` does not already disambiguate — even across a workspace,
        # whose crates sit in different directories.
        if "src" in parts:
            parts = parts[len(parts) - 1 - parts[::-1].index("src"):][1:]
    else:
        while parts and parts[0] in _PATH_ROOTS.get(lang, ()):
            parts = parts[1:]
    if mode == "package":
        # a Go symbol qualifies as `store.Store` — the package name, which is the
        # containing directory. The full import path needs go.mod and is not how
        # source refers to symbols.
        return parts[-2:-1]
    if parts and parts[-1] == _INDEX_FILE.get(lang):
        parts = parts[:-1]            # mod.rs / init.lua / __init__.py name the dir
    return parts


def _declared_scope(lang: str, container: Sequence[str]) -> list[str]:
    """The nesting the source declares, cleaned. `local_name` reduces a Rust
    `impl Type` and a Lua module-table prefix; anything that survives it and still is
    not an identifier is an LSP artifact (a Lua `for in` loop reported as a container)
    and is dropped rather than rendered as a scope."""
    out = []
    for c in container or ():
        n = local_name(c, lang)
        if n and n.isidentifier():
            out.append(n)
    return out


def scope_of(lang: str, file: str, container: Sequence[str]) -> list[str]:
    """The language's own qualified context for a symbol — what a developer writing
    that language would say it belongs to, WITHOUT the leaf name.

    For a FILE-SCOPED language it is the file itself, as a single segment — the file
    is what qualifies the symbol, and a bare `main` is not unique while
    `bin/sharedserver-watcher.c` + `main` is.

    Distinct from the container chain that gives an id its within-file uniqueness. A
    zsh function declared inside another is nested in LOCATION only — zsh functions
    are global wherever they are declared — so it earns an id segment and no scope."""
    if lang in FILE_SCOPED:
        return [file] if file else []      # one segment: the whole relpath
    return _path_scope(lang, file) + _declared_scope(lang, container)


# ── locations, and the edge refs that carry them ──────────────────────────────

def encode_loc(project: str | None, relpath: str) -> str:
    """A file location as it is written inside an edge ref: bare when it belongs to
    the project being indexed, `proj:relpath` when it belongs to a `.crib` ref."""
    return f"{project}:{relpath}" if project else relpath


def decode_loc(loc: str, default_project: str) -> tuple[str, str]:
    """`(project, relpath)` — the inverse of `encode_loc`, resolving a bare location
    against the project it was read from."""
    if ":" in loc:
        proj, _, rel = loc.partition(":")
        return proj, rel
    return default_project, loc


def encode_edge(name: str, loc: str) -> str:
    """One call/reference edge: the counterpart's bare NAME plus where it lives.

    The name alone would not identify it (two files may hold a `main`) and the
    qualified name is not available when the edge is written — the target file may
    not be indexed yet, so this is a DEFERRED reference, resolved at read time
    against `(name, file)`."""
    return f"{name} [{loc}]" if loc else name


def decode_edge(ref: str, default_project: str) -> tuple[str, str, str, str]:
    """`(project, name, relpath, raw_loc)` for one edge ref. `raw_loc` is what was
    written down, kept verbatim for the caller that has to echo an unresolvable
    target back rather than resolve it."""
    name, _, rest = ref.partition(" [")
    loc = rest.rstrip("]")
    proj, rel = decode_loc(loc, default_project)
    return proj, name.strip(), rel, loc


def edge_is_from(ref: str, relpath: str) -> bool:
    """Whether this edge originates in `relpath` — the test a single-file reindex
    uses to strip the edges it is about to re-add."""
    return ref.endswith(f"[{relpath}]")


# ── cross-project symbol handles ──────────────────────────────────────────────

def qualified_symbol(project: str | None, fqname: str) -> str:
    """A symbol handle that carries its project — `dep:util.helper`.

    DELIBERATELY a different function from `encode_loc`, which renders the same
    `a:b` shape for a project and a FILE. One spelling, two meanings, and the only
    thing that ever told them apart was which function you were standing in."""
    return f"{project}:{fqname}" if project else fqname


# ── on-disk basenames ─────────────────────────────────────────────────────────

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def ref_slug(ref: str) -> str:
    """reference → a filesystem- and git-sync-safe basename (no extension), for BOTH
    stores (`symbol_index/*.toml` and `learnings/*.md`). Whitelist `[A-Za-z0-9._-]`;
    everything else (`/` `#` `::` `<>` spaces …) collapses to `-`.

    A short hash of the case-SENSITIVE input is appended whenever the munge is
    lossy or the slug carries uppercase (macOS/Windows are case-insensitive, so
    `mod.Chunk` and `mod.chunk` would be one path). For a REFERENCE that is always:
    one carries `/` or `#` by construction, so every canonical filename is hashed
    and `basename == ref_slug(key)` is a uniform check with no was-this-hashed
    branch. The unhashed pass-through survives only for bare legacy inputs, which
    still name real files on disk."""
    safe = _SLUG_UNSAFE.sub("-", ref).strip("-")
    if safe != ref or safe != safe.lower():
        safe = f"{safe}-{hashlib.sha1(ref.encode()).hexdigest()[:8]}"
    return safe


def legacy_ref_slug(binding: str) -> str:
    """The pre-case-hash slug (`ref_slug` before the APFS fix), so records and
    learning notes ALREADY on disk under the old name are still found. Nothing
    writes this name any more; `SymbolIndex.write` migrates one to the new name as
    it rewrites, and the learnings verbs read through to it in place."""
    safe = _SLUG_UNSAFE.sub("-", binding).strip("-")
    if safe != binding:
        safe = f"{safe}-{hashlib.sha1(binding.encode()).hexdigest()[:8]}"
    return safe




def display_name(entry: dict[str, Any]) -> str:
    """What a developer writing this language would CALL the symbol — its own scope
    joined to its name in its own separator.

    A LABEL, never a key. It can collide where a language has no namespace (every C
    `main` renders the same), so it is what a reader is shown and never what a
    consumer keys on; `id` is for that. Falls back to the qualified name when no
    scope was computed, so an entry from a store written before `scope` existed
    still renders."""
    scope, name = entry.get("scope") or [], entry.get("name") or ""
    if not scope or not name:
        return fqn_of(entry) or str(name)
    return fqname_sep(str(entry.get("lang") or "")).join([*scope, name])


def path_matches(file: str, partial: str) -> bool:
    """Whether `partial` is a trailing run of `file`'s path segments — `state.rs`,
    `core/state.rs` and the whole relpath all select it, `re.rs` does not. Boundary
    on `/`, so a partial can never match mid-segment."""
    if not partial:
        return True
    f, q = file.strip("/").split("/"), partial.strip("/").split("/")
    return len(q) <= len(f) and f[-len(q):] == q


def scope_matches(scope: Sequence[str], partial: str) -> bool:
    """Whether `partial` is a trailing run of the scope chain — `ServerState` and
    `state::ServerState` both select `core::state::ServerState`. Either separator is
    accepted on the way in, since the caller need not know which one stored it."""
    if not partial:
        return True
    q = [s for s in _ANY_SEP.split(partial) if s]
    sc = list(scope or [])
    return len(q) <= len(sc) and sc[-len(q):] == q


def constrain(entries: Sequence[dict[str, Any]], path: str = "", scope: str = "",
              lang: str = "") -> list[dict[str, Any]]:
    """Narrow candidates by the axes a caller might actually know.

    A caller reading a stack trace knows the PATH; one reading source knows the
    SCOPE; neither necessarily knows the qualified name crib stored. Every field
    given is a constraint, so more of them means fewer candidates — which is how an
    ambiguous name becomes unique without the caller having to reconstruct a
    spelling."""
    out = list(entries)
    if lang:
        out = [e for e in out if e.get("lang") == lang]
    if path:
        out = [e for e in out if path_matches(str(e.get("file") or ""), path)]
    if scope:
        out = [e for e in out if scope_matches(e.get("scope") or [], scope)]
    return out




def id_parts(ref: str) -> tuple[str, str]:
    """`(path, tail)` — the inverse of `symbol_ref`. A handle with no `#` is a legacy
    qualified name and returns an empty path, so a reader can tell the two apart
    without guessing."""
    if "#" not in ref:
        return "", ref
    path, _, tail = ref.partition("#")
    return path, tail

def declared_tail(container: Sequence[str], name: str, lang: str = "") -> str:
    """The part of a symbol's name the FILE does not already tell you — its declared
    nesting plus the leaf, in the language's own separator.

    Cleaned: `local_name` reduces a Rust `impl Type`, Lua module-table vars drop, and
    anything still not an identifier is an LSP artifact (a Lua `for in` loop reported
    as a container). Verified across 6626 symbols that this cleaning introduces no
    collisions the raw chain would have avoided."""
    chain = [local_name(c, lang) for c in container or ()]
    chain = [c for c in chain if c and c not in LUA_TABLE_VARS and c.isidentifier()]
    return fqname_sep(lang).join([*chain, name]) if name else fqname_sep(lang).join(chain)


def symbol_ref(path: str, container: Sequence[str], name: str,
               lang: str = "") -> str:
    """A symbol's REFERENCE: the file, and the part of its name the file does not say.

        crib/chunk.py#Chunk.store
        rust/src/core/state.rs#ServerState::exit_code
        core/plugin-bundles/omz.zsh#_zdot_load_omz_lib._omz_async_callback

    Unique across every indexed project (verified: 0 collisions in 6626 symbols).
    The `#` is deliberately not identifier punctuation — it reads as "this file, at
    this symbol", and nothing about it invites pasting into code."""
    tail = declared_tail(container, name, lang)
    return f"{path}#{tail}" if path else tail


def fqn(scope: Sequence[str], name: str, lang: str = "", path: str = "",
        container: Sequence[str] = ()) -> str:
    """The ONE language-specific qualified name — what a developer writing that
    language would actually call the symbol.

        python  crib.chunk.Chunk.store
        rust    core::state::ServerState::exit_code
        lua     sharedserver.health.check_lockdir
        zsh/c   core/plugin-bundles/omz.zsh#_zdot_load_omz_lib._omz_async_callback

    For a FILE-SCOPED language the file is the scope, so the fqn and the
    `symbol_ref` coincide — that is what "the file is the scope" means, not a
    special case. Qualifying by BASENAME instead was measured and rejected: two
    files named `helpers.zsh` in one repo make `helpers.zsh#script_name` ambiguous,
    while the path never is."""
    if lang in FILE_SCOPED:
        return symbol_ref(path, container, name, lang)
    return fqname_sep(lang).join([*(scope or []), name]) if scope else name


def key(entry: dict[str, Any]) -> str:
    """This entry's CURRENT identity — the stored `symbol_ref`, or the one derived
    from its parts when the store predates that field.

    Derivable is the load-bearing half. A conversion runs over many sessions and can
    be interrupted anywhere, so a reader must be able to identify an entry that has
    not been converted yet — otherwise every join has to know whether the store is
    half-done, and every join gets it wrong differently. Here the answer is the same
    string either way, so nothing downstream can tell."""
    ref = entry.get("symbol_ref")
    if ref:
        return str(ref)
    return symbol_ref(str(entry.get("file") or ""), entry.get("container") or (),
                      str(entry.get("name") or ""), str(entry.get("lang") or ""))


def fqn_of(entry: dict[str, Any]) -> str:
    """This entry's NAME — the stored `fqn`, or the one derived from its parts when
    the store predates that field. The name-side twin of `key`: both exist so that a
    reader never has to know which shape a record was written at, and NEITHER ever
    answers with the retired key."""
    got = entry.get("fqn")
    if got:
        return str(got)
    lang = str(entry.get("lang") or "")
    file = str(entry.get("file") or "")
    container = tuple(entry.get("container") or ())
    name = str(entry.get("name") or "")
    return fqn(scope_of(lang, file, container), name, lang, file, container)


def bindings(entry: dict[str, Any]) -> list[str]:
    """EVERY spelling this entry answers to — its key first, then prior ones.

    THE one place that enumerates spellings. Everything else keys on `key(entry)`;
    the fallbacks live here so no read path has to branch on which format it got, and
    "no read path branches on a spelling" is the property that says this migration is
    finished (see docs/plans/symbol-ref-conversion.md §5).

    Prior spellings come from `symbol_was`, falling back to the legacy `fqname` field
    for a store written before that rename. `symbol_was` is populated by CONVERSION,
    not by derivation, so a symbol first indexed afterwards has none — it never
    answered to another name, and inventing one would make a synthetic binding look
    like history."""
    was = entry.get("symbol_was")
    if was is None:
        fq = entry.get("fqname")
        was = [fq] if fq else []
    out = [key(entry)]
    for w in was:
        s = str(w)
        if s and s not in out:
            out.append(s)
    return out


# The file whose own name is its DIRECTORY's namespace rather than a segment of it.
_INDEX_FILE_NAME = {"rust": "mod", "lua": "init", "python": "__init__"}


def canonical(entry: dict[str, Any]) -> tuple[list[str], list[str]]:
    """A symbol's ONE canonical form: `(path segments, declared tail segments)`.

    Everything a caller might type is a TRAILING RUN of `path + tail`. That is not a
    convenience — it is why the qualified spellings ever looked different in the first
    place. `fqn` drops the leading path segments the language does not treat as a
    namespace; the legacy key kept them; a reference pins the boundary between the two
    with `#`. All three are windows onto the same sequence:

        file  rust/src/cli/commands/check.rs   name execute
        run   rust · src · cli · commands · check · execute
              └─────────── path ───────────┘   └── tail ──┘
        legacy key  rust::src::cli::commands::check::execute   the whole run
        fqn                    cli::commands::check::execute   a trailing run
        reference   rust/src/cli/commands/check.rs#execute     boundary pinned
        bare name                               execute        a trailing run of 1

    Measured across 5720 indexed symbols: the legacy key is a trailing run for 5718
    (the two misses are Lua `for in` blocks the LSP reports as containers, whose
    legacy key was never a real name), and the bare name for all 5720.

    So resolution needs no tier per spelling, and no stored history: a retired name
    RESOLVES because it is still a run of the same sequence, not because a field
    remembers it. `symbol_was` stays for the learnings JOIN, where a note holds a
    literal string that has to be matched as written.

    The index file (`__init__.py`, `mod.rs`, `init.lua`) drops out, because it names
    its directory rather than adding a segment — `crib/__init__.py#__version__` is
    `crib.__version__`, not `crib.__init__.__version__`."""
    lang = str(entry.get("lang") or "")
    parts = list(Path(str(entry.get("file") or "")).with_suffix("").parts)
    if parts and parts[-1] == _INDEX_FILE_NAME.get(lang):
        parts = parts[:-1]
    name = str(entry.get("name") or "")
    tail = declared_tail(entry.get("container") or (), name, lang)
    # NAME-AUTHORITATIVE: the recorded name is the final segment, never re-derived by
    # splitting. A zsh function may legally be called `git.push`, and cutting there
    # would invent a boundary that was never in the source — after which `push` would
    # match a symbol not called `push`.
    return parts, _own_segments(tail, name, lang)


def _is_trailing(run: Sequence[str], q: Sequence[str]) -> bool:
    return bool(q) and len(q) <= len(run) and list(run[-len(q):]) == list(q)


def _path_query_segments(qp: str) -> list[str]:
    """A query's path part, cut the way `canonical` cuts a file path — extension
    stripped, so `retrieve.py#foo` meets the `retrieve` the run actually holds."""
    segs = [s for s in qp.strip("/").split("/") if s]
    return [*segs[:-1], Path(segs[-1]).stem] if segs else []


def match_entry(entry: dict[str, Any], query: str) -> str | None:
    """Which TIER `query` matches this entry on — `ref` / `exact` / `suffix` / `name`.

    ONE rule over the canonical form (see `canonical`), not a tier per spelling. The
    tier is only a DISCLOSURE of how much the caller pinned down; it is not a separate
    matching strategy, which is what let a resolver read one field and silently stop
    matching everything else the day that field moved."""
    if not query:
        return None
    path_segs, tail_segs = canonical(entry)
    name = str(entry.get("name") or "")

    if "#" in query:
        qp, _, qt = query.partition("#")
        if qp and not _is_trailing(path_segs, _path_query_segments(qp)):
            return None
        if not qt:
            return None
        # the recorded NAME wins before splitting: a zsh function may legally be
        # called `git.push`, and cutting the query there would fail to match the
        # symbol it names exactly — while `#push` still correctly does not.
        if qt == name or _is_trailing(tail_segs, [s for s in _ANY_SEP.split(qt) if s]):
            return "ref"
        return None

    run = [*path_segs, *tail_segs]
    q = [s for s in _ANY_SEP.split(query) if s]
    if query == name:
        return "name"
    if _is_trailing(run, q):
        return "exact" if len(q) == len(run) else ("name" if len(q) == 1 else "suffix")
    # A RECORDED prior binding, matched exactly. Almost every legacy spelling is
    # already a trailing run of the canonical form — measured, 5718 of 5720 — because
    # it too was derived from the path. The exceptions are the ones no rule can
    # reproduce: a name built from something the entry no longer carries (an LSP
    # `for in` block once reported as a container), or a hand-written binding. Those
    # exist as STRINGS or not at all, so this is the fallback, not the mechanism.
    #
    # Exact only. Prior spellings are a compatibility surface, not a search space:
    # matching one loosely would resurrect the ambiguity the new key exists to remove.
    return "was" if any(str(w) == query for w in (entry.get("symbol_was") or ())) \
        else None


