"""Bundles: a self-contained, disposable crib root handed to a sandbox.

`crib bundle create DIR --overlay <base>:<name> [--context <p>]...` writes DIR
(design "Transient projects, out-of-store homes, and code overlays", parts 3,
6, 7, 9):

    DIR/bundle.toml                  manifest: expiry, overlay, context projects
    DIR/config/config.toml           embedded store, writable = [overlay],
                                     [locations] CONTEXT for the context copies
    DIR/config/{lsp.json,facets.toml}  the trusted side's, copied
    DIR/data/projects/<base>@<name>/ the OVERLAY: a writable, transient copy of
                                     <base> (its pillars, symbol records, and a
                                     trusted .crib), expiring with the bundle
    DIR/data/projects/<p>/           a CONTEXT project's stub + symbol records;
                                     its notes live in the context copy
    <context dir>/<p>/               the context copies (read-only mount in the
                                     sandbox: default DIR/context)

The sandbox opens it with `crib --root DIR [--store SCRATCH]`: writes go only
to the overlay (`writable`), and only under DIR (confined mode).

The trusted side REGISTERS each bundle (`<data>/bundles.json`) and owns its
deletion: `crib bundle delete`, or the daemon's expiry sweep when the
manifest's `expires` passes. The manifest is writable inside the sandbox (a
session extends the expiry with `crib bundle expire`), so ONLY the expiry is
read back from it; which directories a delete removes comes from the trusted
registry, never from the manifest.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

from .errors import CribUserError
from .paths import Paths, check_project_name

MANIFEST = "bundle.toml"
REGISTRY = "bundles.json"
VECTORS_FILE = "embed-cache.parquet"  # the bundle's vectors, under DIR/data
FORMAT = 1
_MANIFEST_MAX = 64 * 1024  # a manifest is a few lines; anything bigger is hostile
# the cap a registry entry with no `max_expires` (one written by 0.28.0-dev1)
# gets: `bundle create`'s default --max-retention, counted from its creation
DEFAULT_RETENTION = datetime.timedelta(days=30)
# bundles the sweep failed to delete and has said so (once per bundle per process)
_SWEEP_WARNED: set[str] = set()


# ── manifest ─────────────────────────────────────────────────────────────────


def _toml_str(v: str) -> str:
    return json.dumps(v)  # a JSON string is a valid TOML basic string


def _read_small(f: Path, limit: int = _MANIFEST_MAX) -> str:
    """A file the SANDBOX can rewrite, read safely: a regular file only (not a
    symlink, not a FIFO that would block), at most `limit` bytes."""
    import stat

    try:
        fd = os.open(f, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise CribUserError(f"{f}: not a readable regular file ({e})") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise CribUserError(f"{f}: not a regular file")
        if st.st_size > limit:
            raise CribUserError(f"{f}: larger than {limit} bytes")
        return os.read(fd, limit + 1).decode("utf-8", errors="strict")
    except UnicodeDecodeError as e:
        raise CribUserError(f"{f}: not UTF-8 text") from e
    finally:
        os.close(fd)


def _replace_text(f: Path, text: str) -> None:
    """Write `f` atomically, replacing (never writing through) whatever is
    there: a symlink planted at `f` is replaced, not followed."""
    import tempfile

    fd, tmp = tempfile.mkstemp(dir=f.parent, prefix=f".{f.name}.")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
        os.replace(tmp, f)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _no_symlink_between(base: Path, target: Path) -> bool:
    """`target` lies under `base` with no symlink on the way (so a write to it
    can't be redirected outside the bundle)."""
    try:
        rel = target.relative_to(base)
    except ValueError:
        return False
    p = base
    for part in rel.parts:
        p = p / part
        if p.is_symlink():
            return False
    return True


def write_manifest(bundle: Path, m: dict[str, Any]) -> None:
    lines = [f"format = {FORMAT}"]
    for key in ("created", "expires", "overlay", "base"):
        if m.get(key):
            lines.append(f"{key} = {_toml_str(str(m[key]))}")
    lines.append(
        "context = [" + ", ".join(_toml_str(p) for p in m.get("context", [])) + "]"
    )
    _replace_text(bundle / MANIFEST, "\n".join(lines) + "\n")


def read_manifest(bundle: Path) -> dict[str, Any]:
    f = bundle / MANIFEST
    try:
        data = tomllib.loads(_read_small(f))
    except tomllib.TOMLDecodeError as e:
        raise CribUserError(f"{f}: not a readable bundle manifest ({e})") from e
    if data.get("format") != FORMAT:
        raise CribUserError(f"{f}: unknown bundle format {data.get('format')!r}")
    return data


def manifest_expiry(bundle: Path) -> datetime.datetime | None:
    """The bundle's CURRENT expiry, strictly parsed (the one field the trusted
    side reads back from a manifest a sandbox may have rewritten)."""
    from . import expiry

    return expiry.when(read_manifest(bundle).get("expires"))


# ── registry (trusted side) ──────────────────────────────────────────────────


def _registry_file(paths: Paths) -> Path:
    return paths.data_dir / REGISTRY


def registry(paths: Paths) -> dict[str, dict[str, Any]]:
    f = _registry_file(paths)
    try:
        data = json.loads(f.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        raise CribUserError(f"{f}: unreadable bundle registry ({e})") from e
    return data if isinstance(data, dict) else {}


def _save_registry(paths: Paths, reg: dict[str, dict[str, Any]]) -> None:
    f = _registry_file(paths)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
    tmp.replace(f)


# ── create ───────────────────────────────────────────────────────────────────

# Never copied: version rings, stubs and .crib (the bundle writes its own),
# and `.source_root`, the TRUSTED machine's repo path: in the sandbox it would
# be revalidated against (records dropped, or trusted code read). The overlay
# gets the checkout as its source root on its first `project index`.
_COPY_IGNORE = shutil.ignore_patterns(
    ".versions", ".cribproject", ".gitignore", ".crib", "__pycache__", ".source_root"
)


def _projects(paths: Paths) -> list[str]:
    pd = paths.projects_dir
    return sorted(p.name for p in pd.iterdir() if p.is_dir()) if pd.is_dir() else []


def _registered_root(paths: Paths, project: str) -> Path | None:
    """Where `project` was last indexed from (as Crib._registered_root)."""
    from .codeindex import SymbolIndex
    from .sources import SourceRoots

    pdir = paths.project_dir(project)
    root = SymbolIndex(pdir).source_root()
    if root is None:
        docs = {Path(r) for r in SourceRoots(pdir / "doc-sources.json").all().values()}
        root = docs.pop() if len(docs) == 1 else None
    return root.resolve() if root is not None and root.is_dir() else None


def _copy_project_files(
    paths: Paths, cfg: Any, proj: str, data_dest: Path, records_dest: Path
) -> int:
    """Copy `proj`'s pillar trees into `data_dest` and its symbol records into
    `records_dest/symbol_index`; how many files."""
    from .paths import resolve_project_paths
    from .watch import PILLAR_SEGMENTS

    pp = resolve_project_paths(paths, cfg, proj)
    n = 0
    for seg in PILLAR_SEGMENTS:
        src = pp.pillar_dir(seg)
        if src.is_dir():
            shutil.copytree(
                src, data_dest / seg, ignore=_COPY_IGNORE, dirs_exist_ok=True
            )
            n += sum(1 for p in (data_dest / seg).rglob("*") if p.is_file())
    sym = paths.project_dir(proj) / "symbol_index"
    if sym.is_dir():
        shutil.copytree(
            sym, records_dest / "symbol_index", ignore=_COPY_IGNORE, dirs_exist_ok=True
        )
        n += sum(1 for p in (records_dest / "symbol_index").rglob("*") if p.is_file())
    return n


def _copy_lsp_json(src: Path, dest: Path) -> dict[str, str]:
    """Copy the trusted machine's lsp.json into the bundle with every server
    `command` that is a path reduced to its bare name (`/opt/homebrew/bin/gopls`
    -> `gopls`): the bundle runs on another machine (the review image), where a
    bare name is looked up on PATH (codeindex.resolve_command) and a path from
    here would not exist, silently disabling that server. Returns the rewrites,
    label -> the command it had. A malformed file is not copied (crib would
    ignore it anyway)."""
    if not src.is_file():
        return {}
    try:
        specs = json.loads(src.read_text())
    except (ValueError, OSError) as e:
        print(f"[crib] {src} not copied into the bundle: {e}", file=sys.stderr)
        return {}
    if not isinstance(specs, dict):
        print(f"[crib] {src} not copied into the bundle: not a JSON object",
              file=sys.stderr)
        return {}
    rewritten: dict[str, str] = {}
    for label, spec in specs.items():
        cmd = spec.get("command") if isinstance(spec, dict) else None
        if isinstance(cmd, str) and "/" in cmd:
            spec["command"] = cmd.rstrip("/").rsplit("/", 1)[-1]
            rewritten[str(label)] = cmd
    dest.write_text(json.dumps(specs, indent=2) + "\n")
    return rewritten


def _trusted_crib(paths: Paths, base: str, overlay: str) -> str | None:
    """The base repo's `.crib`, renamed to the overlay: code `paths:` and `docs:`
    only (confined mode ignores residency and mappings anyway)."""
    from .config import CribLink

    root = _registered_root(paths, base)
    link = CribLink.find(root) if root is not None else None
    if link is None:
        return None
    lines = [f"project: {overlay}"]
    if link.paths:
        lines += ["paths:", *[f"  - {json.dumps(g)}" for g in link.paths]]
    if link.docs:
        lines += ["docs:", *[f"  - {json.dumps(g)}" for g in link.docs]]
    return "\n".join(lines) + "\n"


def create(
    paths: Paths,
    cfg: Any,
    bundle: Path,
    overlay: str,
    context: list[str] | None = None,
    context_to: Path | None = None,
    context_mount: str | None = None,
    expires: str | None = "7d",
    vectors: bool = True,
    max_retention: str = "30d",
) -> dict[str, Any]:
    """Write a bundle at `bundle` (see the module docstring) and register it.

    `overlay` is `<base>:<name>`: the base project copied as the writable,
    transient project `<base>@<name>`. `context` projects ride along read-only:
    their files go to `context_to` (default `bundle/context`), which the
    sandbox mounts read-only, at `context_mount` (default: `$ROOT/context`, i.e.
    inside the bundle wherever it is mounted)."""
    from . import expiry

    if paths_root := paths.root:
        raise CribUserError(
            f"bundles are created on the trusted side, not in "
            f"confined mode (--root {paths_root})"
        )
    base, sep, name = overlay.partition(":")
    if not sep or not base or not name:
        raise CribUserError("--overlay takes <base>:<name>, e.g. toucan:pr-123")
    base = check_project_name(base)
    proj = check_project_name(f"{base}@{name}")
    context = [check_project_name(p) for p in (context or [])]
    for p in (base, *context):
        if p not in _projects(paths):
            raise CribUserError(f"no project {p!r} to bundle")
    if proj in context:
        raise CribUserError("the overlay can't also be a context project")
    bundle = bundle.expanduser().resolve()
    if bundle.exists() and any(bundle.iterdir()):
        raise CribUserError(f"{bundle} exists and isn't empty")
    if str(bundle) in registry(paths):
        raise CribUserError(f"{bundle} is already a registered bundle")
    ctx_dir = context_to.expanduser().resolve() if context_to else bundle / "context"
    if context and context_to and ctx_dir.exists() and any(ctx_dir.iterdir()):
        raise CribUserError(f"{ctx_dir} exists and isn't empty")
    # The HARD cap, held only in the trusted registry: the manifest (rewritable
    # in the sandbox) can shorten or extend the expiry, never past this.
    cap = expiry.parse(max_retention)
    until = expiry.parse(expires) if expires else cap
    u_at, cap_at = expiry.when(until), expiry.when(cap)
    capped = u_at is not None and cap_at is not None and u_at > cap_at
    if capped:
        until = cap
    # Directories this create owns: the bundle (absent, or checked empty above)
    # and, only when context projects are copied to --context-to, that dir
    # (likewise absent or checked empty). Nothing else is ever cleaned up: a
    # --context-to given without --context is not touched at all.
    owned = [(bundle, not bundle.exists())]
    if context and context_to:
        owned.append((ctx_dir, not ctx_dir.exists()))
    try:
        return _build(paths, cfg, bundle, base, proj, context, ctx_dir, context_to,
                      context_mount, until, vectors, cap, capped)
    except BaseException:
        # a half-built bundle is unregistered, so nothing would ever clean it up
        # (and a retry would refuse the non-empty dir): remove what was made
        for d, made in owned:
            if d.is_dir():
                if made:
                    shutil.rmtree(d, ignore_errors=True)
                else:
                    for child in d.iterdir():
                        (shutil.rmtree(child, ignore_errors=True) if child.is_dir()
                         and not child.is_symlink() else child.unlink(missing_ok=True))
        raise


def _build(paths: Paths, cfg: Any, bundle: Path, base: str, proj: str,
           context: list[str], ctx_dir: Path, context_to: Path | None,
           context_mount: str | None, until: str | None, vectors: bool,
           cap: str | None = None, capped: bool = False
           ) -> dict[str, Any]:
    """`create`'s body, once the arguments are checked."""
    from .config import ProjectConfig

    now = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")

    bpaths = Paths(
        config_dir=bundle / "config",
        data_dir=bundle / "data",
        index_dir=bundle / "index",
    )
    bpaths.config_dir.mkdir(parents=True)
    bpaths.projects_dir.mkdir(parents=True)
    files: dict[str, int] = {}

    # the overlay: a writable, transient copy of the base
    pdir = bpaths.project_dir(proj)
    pdir.mkdir(parents=True)
    files[proj] = _copy_project_files(paths, cfg, base, pdir, pdir)
    ProjectConfig(name=proj, created=now, transient=True, expires=until).save(
        pdir / ".cribproject"
    )
    (pdir / ".gitignore").write_text("*\n")
    if (trusted := _trusted_crib(paths, base, proj)) is not None:
        (pdir / ".crib").write_text(trusted)

    # context projects: each WHOLE (stub, notes, records) in the context dir,
    # the bundle's read-only second projects dir; nothing of them in the root
    for p in context:
        home = ctx_dir / p
        home.mkdir(parents=True)
        files[p] = _copy_project_files(paths, cfg, p, home, home)
        ProjectConfig(name=p, created=now).save(home / ".cribproject")

    mount = context_mount or ("$ROOT/context" if not context_to else str(ctx_dir))
    lines = [
        "# A crib BUNDLE: open with `crib --root <this dir> [--store SCRATCH]`.",
        "writable = [" + json.dumps(proj) + "]",
        "",
        "[daemon]",
        "enabled = false  # verbs run in-process; serve MCP with `crib --root . --mcp`",
        "",
        "[duckdb]",
        'mode = "embedded"',
        "",
    ]
    if context:  # the read-only projects dir, as the sandbox sees it
        lines.insert(2, f"context_dir = {json.dumps(mount)}")
    # the sandbox embeds with the SAME model, or its store refuses the vectors
    lines += ["", "[embed]", f"model = {json.dumps(cfg.embed.model)}"]
    (bpaths.config_dir / "config.toml").write_text("\n".join(lines) + "\n")
    lsp_rewritten = _copy_lsp_json(paths.config_dir / "lsp.json",
                                   bpaths.config_dir / "lsp.json")
    for extra in ("grammar.json", "facets.toml"):
        if (src := paths.config_dir / extra).is_file():
            shutil.copy2(src, bpaths.config_dir / extra)

    write_manifest(
        bundle,
        {
            "created": now,
            "expires": until,
            "overlay": proj,
            "base": base,
            "context": context,
        },
    )
    vec_rows = (_build_vectors(paths, cfg, bundle, [proj, *context],
                               ctx_dir if context else None) if vectors else None)
    reg = registry(paths)
    reg[str(bundle)] = {
        "created": now,
        "overlay": proj,
        "context_dir": str(ctx_dir) if context else None,
        "max_expires": cap,
    }
    _save_registry(paths, reg)
    return {
        "bundle": str(bundle),
        "overlay": proj,
        "base": base,
        "context": context,
        "context_dir": str(ctx_dir) if context else None,
        "expires": until,
        "expires_capped": capped,
        "lsp_rewritten": lsp_rewritten,
        "files": files,
        "vectors": vec_rows,
    }


# ── vectors ──────────────────────────────────────────────────────────────────


class _ReadThrough:
    """Embedding-cache storage for building a bundle: the bundle's own store
    first, then the MAIN store's cache (copied in on a hit), so only text never
    embedded anywhere is computed. Writes go to the bundle's store only."""

    def __init__(self, local: Any, upstream: Any) -> None:
        self.local, self.upstream = local, upstream

    def ec_get(self, model: str, hashes: list[str]) -> dict[str, list[float]]:
        got = self.local.ec_get(model, hashes)
        if missing := [h for h in hashes if h not in got]:
            try:
                found = self.upstream.ec_get(model, missing)
            except Exception:  # noqa: BLE001 — the main cache is an optimisation
                found = {}
            if found:
                self.local.ec_put(model, list(found.items()))
                got.update(found)
        return got

    def ec_put(self, model: str, rows: list[tuple[str, list[float]]]) -> None:
        self.local.ec_put(model, rows)


def _build_vectors(paths: Paths, cfg: Any, bundle: Path, projects: list[str],
                   context_dir: Path | None) -> int | None:
    """Index the bundle's projects into its OWN store (DIR/index, ready for a
    sandbox that keeps the store in the root), embedding through the main
    store's cache, then export that store's cache, i.e. exactly the bundle's
    vectors, to DIR/data/embed-cache.parquet: a store rebuilt elsewhere
    (`--store`) loads it and re-embeds nothing. None with the hash embedder
    (nothing worth carrying)."""
    import asyncio

    from .app import Crib, _build_store
    from .config import Config
    from .embedcache import CachedEmbedder
    from .store_duckdb import DuckDBStore

    bpaths = Paths(config_dir=bundle / "config", data_dir=bundle / "data",
                   index_dir=bundle / "index")
    bcfg = Config.load(bpaths.config_file)
    bcfg.locations["ROOT"] = str(bundle)
    if context_dir is not None:  # the copies as they sit HERE, not in the sandbox
        bcfg.context_dir = str(context_dir)
    bcfg.writable = None  # building the bundle's own cache, not a sandbox session
    bpaths.duckdb_file.parent.mkdir(parents=True, exist_ok=True)
    bstore = DuckDBStore.embedded(bpaths.duckdb_file)
    main: Any = None
    close_main: Any = None
    try:
        main, close_main = _build_store(paths, cfg)
    except Exception:  # noqa: BLE001 — e.g. an embedded store a daemon holds
        main = None
    try:
        crib = Crib(bpaths, bcfg, bstore)
        if not isinstance(crib.embedder, CachedEmbedder):
            return None
        if main is not None:
            crib.embedder.use_storage(_ReadThrough(bstore, main))

        async def index_all() -> None:
            for p in projects:
                await crib.reindex(project=p)
                crib.code.refresh(p)
                crib.code.fill_vectors(p)

        asyncio.run(index_all())
        return bstore.ec_export(bpaths.data_dir / VECTORS_FILE)
    finally:
        bstore.close()
        if close_main is not None:
            close_main()


# ── lifecycle ────────────────────────────────────────────────────────────────


def expire(bundle: Path, when: str, paths: Paths | None = None) -> dict[str, Any]:
    """Set a bundle's expiry.

    In confined mode (the sandbox, extending its own root) the manifest and the
    overlay's stub are rewritten. On the TRUSTED side (`paths`, not confined)
    the bundle must be registered, and only the manifest and the registry are
    written: the registry's cap moves with the operator's expiry, and the stub
    (inside the sandbox's writable tree, whose directories it can swap for
    symlinks between a check and a write) is never touched; nothing trusted
    reads it."""
    from . import expiry
    import yaml

    from .config import ProjectConfig

    bundle = bundle.expanduser().resolve()
    if paths is None or paths.root is not None:
        root = paths.root if paths is not None else None
        if root is not None and bundle != root:
            raise CribUserError(f"confined mode: only this bundle ({root}) can be extended")
        overlay = read_manifest(bundle).get("overlay")
    else:
        reg = registry(paths)
        entry = reg.get(str(bundle))
        if entry is None:
            raise CribUserError(f"{bundle} is not a registered bundle (`crib bundle list`)")
        until = expiry.parse(when)
        m = read_manifest(bundle)
        m["expires"] = until
        write_manifest(bundle, m)
        entry["max_expires"] = until  # the operator sets the cap, up or down
        _save_registry(paths, reg)
        return {"bundle": str(bundle), "expires": until}
    m = read_manifest(bundle)
    until = expiry.parse(when)
    m["expires"] = until
    write_manifest(bundle, m)
    if isinstance(overlay, str):
        stub = bundle / "data" / "projects" / check_project_name(overlay) / ".cribproject"
        if not _no_symlink_between(bundle, stub):
            raise CribUserError(f"{stub}: reached through a symlink; not written")
        if stub.exists() or stub.is_symlink():
            # read and written as the manifest is: a small regular file, never
            # through a symlink (the stub lives in the sandbox's writable root)
            try:
                pcfg = ProjectConfig.loads(_read_small(stub), overlay, where=str(stub))
            except yaml.YAMLError as e:
                raise CribUserError(f"{stub}: not readable YAML ({e})") from e
            pcfg.expires = until
            _replace_text(stub, pcfg.dumps())
    return {"bundle": str(bundle), "expires": until}


def _entry_cap(entry: dict[str, Any]) -> datetime.datetime | None:
    """A registry entry's hard cap: its `max_expires`, or for an entry from
    before caps (0.28.0-dev1) its creation plus DEFAULT_RETENTION."""
    from . import expiry

    if (cap := expiry.when(entry.get("max_expires"))) is not None:
        return cap
    if (made := expiry.when(entry.get("created"))) is not None:
        return made + DEFAULT_RETENTION
    return None


def listing(paths: Paths, now: datetime.datetime | None = None) -> list[dict[str, Any]]:
    """Registered bundles with their state (live/due/missing/unreadable) and
    effective expiry: the manifest's, capped at the registry's `max_expires`."""
    now = now or datetime.datetime.now(datetime.UTC)
    out = []
    for path, entry in sorted(registry(paths).items()):
        row: dict[str, Any] = {"bundle": path, **entry}
        b = Path(path)
        cap = _entry_cap(entry)
        if not b.is_dir():
            row["state"] = "missing"
        else:
            try:
                at = manifest_expiry(b)
            except CribUserError as e:
                # unreadable (perhaps on purpose): due once the cap passes
                row["state"] = "due" if cap and cap <= now else "unreadable"
                row["error"] = str(e)
            else:
                if cap and (at is None or at > cap):
                    at = cap  # the manifest can't extend past the trusted cap
                row["expires"] = at.isoformat(timespec="seconds") if at else None
                row["state"] = "due" if at and at <= now else "live"
        out.append(row)
    return out


def delete(paths: Paths, bundle: Path) -> dict[str, Any]:
    """Remove a REGISTERED bundle: its directory, and its context copy when that
    lives elsewhere (paths from the trusted registry only)."""
    key = str(bundle.expanduser().resolve())
    reg = registry(paths)
    entry = reg.get(key)
    if entry is None:
        raise CribUserError(f"{key} is not a registered bundle (`crib bundle list`)")
    removed = []
    for d in (key, entry.get("context_dir")):
        if d and Path(d).is_dir():
            shutil.rmtree(d)
            removed.append(d)
    del reg[key]
    _save_registry(paths, reg)
    return {"bundle": key, "removed": removed}


def sweep(paths: Paths, now: datetime.datetime | None = None) -> list[str]:
    """Delete every registered bundle that is due (the scheduled delete): its
    manifest's expiry, capped by the registry's `max_expires`, has passed; an
    unreadable manifest is left alone until the cap passes."""
    gone = []
    for row in listing(paths, now):
        if row.get("state") == "due":
            try:
                delete(paths, Path(row["bundle"]))
                gone.append(row["bundle"])
            except Exception as e:  # noqa: BLE001 — one bundle never blocks the rest
                if row["bundle"] not in _SWEEP_WARNED:  # once, not every sweep
                    _SWEEP_WARNED.add(row["bundle"])
                    print(f"[crib] expired bundle {row['bundle']} not deleted: {e}",
                          file=sys.stderr)
    return gone
