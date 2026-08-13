"""How a symbol is spelled, encoded, and matched — the one place that decides.

A symbol reaches the outside world in several spellings, and every one of them is a
CONVENTION that a writer and a reader have to agree on:

  * the qualified name          `crib.app.Crib.code_graph`, `a::b::C::d`
  * the edge ref                `helper [src/util.py]`, `helper [dep:src/util.py]`
  * the location inside it      `src/util.py` or `dep:src/util.py`
  * the cross-project handle    `dep:util.helper`
  * the on-disk basename        `learning_slug(fqn)`

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


def match(entry_fq: str, entry_name: str, query: str, lang: str = "") -> str | None:
    """Which TIER `query` matches this entry on — most specific first — or None.

    - `fqname` — the whole qualified name (either separator spelling accepted)
    - `suffix` — a trailing run of its SEGMENTS (`lockfile::ClientsLock`), so a
      partial path disambiguates without typing the root
    - `name`   — the bare local name, from the entry's OWN `name` field. This is
      what makes a bare name work in every language whatever separator qualified
      it, and it matters most where the qualified spelling is one the caller could
      not have guessed (a Rust path rendered from the file tree, not the crate path).

    Comparison is between SEGMENT LISTS, never between re-joined strings: there is
    then no separator character in play to collide with one inside a name, and a
    suffix match is a boundary by construction rather than by string assertion.
    Only the QUERY is read permissively (either separator) — the caller cannot know
    which language stored the symbol, and being generous there only widens what may
    be typed; the result still has to land on real segment boundaries.
    """
    if not query:
        return None
    if entry_fq == query:
        return "fqname"
    seg = segments(entry_fq, lang)
    # the entry's own `name` is the AUTHORITATIVE final segment — a name that
    # contains the separator (a legal zsh `git.push`) would otherwise be split into
    # pieces that were never boundaries, and `push` would match a symbol not called
    # `push`. Rebuild the tail from what the writer recorded rather than re-deriving.
    if entry_name and entry_fq.endswith(entry_name):
        head = entry_fq[:-len(entry_name)].rstrip(fqname_sep(lang) if lang else ".:")
        seg = (segments(head, lang) if head else []) + [entry_name]
    q = [s for s in _ANY_SEP.split(query) if s]
    if not q:
        return None
    if seg == q:
        return "fqname"               # same name, spelled with the other separator
    if len(q) == 1:
        if entry_name and entry_name == query:
            return "name"
        return "suffix" if seg and seg[-1] == query else None
    if len(q) < len(seg) and seg[-len(q):] == q:
        return "suffix"
    return "name" if entry_name and entry_name == query else None


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


def qualify(lang: str, module: str, container: Sequence[str], name: str) -> str:
    """Render a language-idiomatic qualified name from the parts."""
    cont = [local_name(c, lang) for c in container
            if local_name(c, lang) not in LUA_TABLE_VARS]
    pieces = ([module] if module else []) + cont + [name]
    return fqname_sep(lang).join(p for p in pieces if p)


# Whether a file's PATH forms part of the language's own qualified name, and from
# which root:
#   "module"  — the path IS the namespace, file included (python, lua: import/require)
#   "package" — the last DIRECTORY is the namespace; the filename is not (go)
#   "crate"   — namespace is crate-relative: everything after the last `src` (rust)
#   absent    — the path contributes NOTHING. Either the language declares its
#               namespace in the source (c++, ruby) or it has none (c, zsh, sh).
_PATH_NAMESPACE = {"python": "module", "lua": "module", "go": "package",
                   "rust": "crate"}
# Languages with NO namespace concept at all. Declared nesting in these is lexical
# LOCATION, not scope: a zsh function defined inside another is callable globally
# once the outer has run, so it earns an id segment and no scope.
_NO_NAMESPACE = frozenset({"c", "zsh", "sh", "bash", "fish", "make"})
# Source roots that are build layout rather than namespace, stripped from the front.
_PATH_ROOTS = {"python": ("src",), "lua": ("lua",), "go": ("src",)}
# The file whose name is its DIRECTORY's namespace, not its own segment.
_INDEX_FILE = {"rust": "mod", "lua": "init", "python": "__init__"}


def module_of(relpath: str, lang: str) -> str:
    """Module/namespace from the FILE PATH (language-specific). The container parts
    come from documentSymbol; the module part can only come from the path."""
    parts = list(Path(relpath).with_suffix("").parts)
    while parts and parts[0] in ("src", "lua", "lib"):
        parts = parts[1:]
    index_file = {"rust": "mod", "lua": "init"}.get(lang, "__init__")
    if parts and parts[-1] == index_file:
        parts = parts[:-1]
    return fqname_sep(lang).join(parts)


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

    EMPTY for a language with no namespace (C, zsh), and that emptiness is
    information: `main` in `bin/sharedserver-watcher.c` belongs to nothing, and its
    directory is not a substitute.

    Distinct from the container chain that gives an id its within-file uniqueness. A
    zsh function declared inside another is nested in LOCATION only — zsh functions
    are global wherever they are declared — so it earns an id segment and no scope."""
    if lang in _NO_NAMESPACE:
        return []
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


def learning_slug(fqn: str) -> str:
    """fqn → a filesystem- and git-sync-safe basename (no extension). Whitelist
    `[A-Za-z0-9._-]`; everything else (`::` `/` `<>` `*` `&` spaces `~` operators
    …) collapses to `-`.

    A short fqn hash is appended whenever the basename alone can't identify the
    symbol — the exact name stays recoverable from the note's `symbol:`
    frontmatter, which is authoritative regardless:

      • the munge was LOSSY (`core::cache::Store::get` and `core-cache-Store-get`
        would otherwise be one file);
      • the slug contains UPPERCASE. macOS (APFS/HFS+) and Windows are
        case-INSENSITIVE, so `mod.Chunk` and `mod.chunk` are the SAME path there:
        one symbol's record silently overwrote the other's, and the loser's
        learning went with it. The hash is over the case-SENSITIVE fqn, so the two
        land in different files on every platform.

    A clean all-lowercase fqn still passes through verbatim: `crib.notes.load`."""
    safe = _SLUG_UNSAFE.sub("-", fqn).strip("-")
    if safe != fqn or safe != safe.lower():
        safe = f"{safe}-{hashlib.sha1(fqn.encode()).hexdigest()[:8]}"
    return safe


def legacy_learning_slug(fqn: str) -> str:
    """The pre-case-hash slug (`learning_slug` before the APFS fix), so records and
    learning notes ALREADY on disk under the old name are still found. Nothing
    writes this name any more; `SymbolIndex.write` migrates one to the new name as
    it rewrites, and the learnings verbs read through to it in place."""
    safe = _SLUG_UNSAFE.sub("-", fqn).strip("-")
    if safe != fqn:
        safe = f"{safe}-{hashlib.sha1(fqn.encode()).hexdigest()[:8]}"
    return safe


def entry_ref(entry: dict[str, Any], project: str | None = None) -> dict[str, Any]:
    """The structured reference to one indexed symbol — what a caller should be handed
    instead of a string it has to parse. `id` is the key (unique, stable, what edges
    point at and what round-trips as input); the rest are the parts it is built from."""
    return {"id": qualified_symbol(project, entry.get("fqname", "")),
            "fqname": entry.get("fqname", ""), "name": entry.get("name", ""),
            "path": entry.get("file", ""), "lang": entry.get("lang", ""),
            **({"project": project} if project else {})}


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
        return str(entry.get("fqname") or name)
    return fqname_sep(str(entry.get("lang") or "")).join([*scope, name])
