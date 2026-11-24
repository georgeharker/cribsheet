"""CLI output styled by role (crib/style.py), only on a capable terminal.

The invariant every emitter keeps: styling adds escape codes and NOTHING else,
so stripping them gives exactly the unstyled output, and unstyled output (piped,
`--json`, `--color never`) is what it always was.
"""

from __future__ import annotations

import argparse
import io
import re
from types import SimpleNamespace

import pytest

from crib import cli, style

_ESC = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;[^\x1b]*\x1b\\")


def _plain(text: str) -> str:
    return _ESC.sub("", text)


@pytest.fixture(autouse=True)
def _reset_style(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CRIB_THEME_FILE", raising=False)
    yield
    style.configure("never")
    cli._ITEM_PATHS.clear()
    cli._SOURCE_ROOTS.clear()


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_paint_is_the_identity_until_styling_is_on():
    style.configure("never")
    assert style.paint("path", "a.md") == "a.md"
    style.configure("always")
    out = style.paint("path", "a.md")
    assert out != "a.md" and _plain(out) == "a.md"
    assert style.paint("path", "") == ""  # nothing to style


def test_auto_styles_only_a_terminal_and_honours_no_color(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    style.configure("auto", stream=io.StringIO())  # piped
    assert not style.enabled()
    style.configure("auto", stream=_Tty())
    assert style.enabled()
    monkeypatch.setenv("NO_COLOR", "1")
    style.configure("auto", stream=_Tty())
    assert not style.enabled()
    style.configure("always", stream=io.StringIO())  # --color always wins
    assert style.enabled()


def test_a_theme_file_overrides_a_role(tmp_path, monkeypatch):
    theme = tmp_path / "theme.ini"
    theme.write_text("[styles]\ncrib.path = red\nmarkdown.h1 = blue\n")
    monkeypatch.setenv("CRIB_THEME_FILE", str(theme))
    style.configure("always")
    assert "\x1b[31m" in style.paint("path", "a.md")


def test_a_link_wraps_the_text_and_needs_an_absolute_path(tmp_path):
    style.configure("always")
    f = tmp_path / "x.md"
    url = style.file_link(f)
    assert url is not None and url == f.as_uri()
    assert style.file_link("rel/x.md") is None
    out = style.paint("path", "x.md", url)
    assert "\x1b]8;" in out and url in out and _plain(out) == "x.md"


def test_json_is_never_styled(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(
        cli, "_run_inprocess", lambda args, cfg: seen.append(style.enabled())
    )
    cli.main(["--no-daemon", "--json", "--color", "always", "plan", "list"])
    cli.main(["--no-daemon", "--color", "always", "plan", "list"])
    assert seen == [False, True]


def _both(emit) -> tuple[str, str]:
    """(unstyled, styled) stdout of one emitter call."""
    import contextlib

    outs = []
    for mode in ("never", "always"):
        style.configure(mode)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            emit()
        outs.append(buf.getvalue())
    return outs[0], outs[1]


_PLAN_ARGS = SimpleNamespace(cmd="plan", json=False)
_DESIGN_ARGS = SimpleNamespace(cmd="design", json=False)
_SYM = {
    "symbol_ref": "a.py#f",
    "fqn": "a.f",
    "kind": "function",
    "file": "a.py",
    "line": 3,
    "project": "p",
    "description": "does f",
    "calls": ["a.g"],
    "called_by": [],
    "references": [],
    "rank": 1,
    "learning": {"relpath": "a.f.md", "body": "pinned", "stale": True},
}


@pytest.mark.parametrize(
    "name, emit",
    [
        (
            "plan list",
            lambda: cli._emit_plan_list(
                {
                    "project": "p",
                    "items": [
                        {
                            "title": "T",
                            "status": "in-progress",
                            "relpath": "t.md",
                            "group": "in-progress",
                            "blocked": True,
                            "blocked_by": [
                                {"ref": "d.md", "kind": "plan", "status": "todo"}
                            ],
                            "missing_deps": ["x"],
                            "revisit": ["moved"],
                        },
                        {
                            "title": "U",
                            "status": "done",
                            "relpath": "u.md",
                            "group": "done",
                        },
                    ],
                },
                _PLAN_ARGS,
            ),
        ),
        (
            "plan read",
            lambda: cli._emit_plan_read(
                {
                    "project": "p",
                    "title": "T",
                    "status": "todo",
                    "relpath": "t.md",
                    "body": "",
                    "deps": [
                        {
                            "title": "D",
                            "status": "active",
                            "relpath": "d.md",
                            "tainted": True,
                        }
                    ],
                    "blocked_by": [{"ref": "d.md"}],
                    "next": "do it",
                },
                _PLAN_ARGS,
            ),
        ),
        (
            "design read",
            lambda: cli._emit_design_read(
                {
                    "project": "p",
                    "title": "T",
                    "status": "proposed",
                    "relpath": "t.md",
                    "tainted": True,
                    "body": "",
                    "causes": [{"change_kind": "edit", "reason": "r"}],
                    "dependents": [
                        {"title": "E", "status": "superseded", "relpath": "e.md"}
                    ],
                },
                _DESIGN_ARGS,
            ),
        ),
        (
            "design list",
            lambda: cli._emit_design_list(
                {
                    "project": "p",
                    "total": 2,
                    "designs": [
                        {
                            "title": "T",
                            "status": "superseded",
                            "relpath": "t.md",
                            "deps": 1,
                        },
                        {
                            "title": "U",
                            "status": "proposed",
                            "relpath": "u.md",
                            "tainted": True,
                        },
                    ],
                },
                _DESIGN_ARGS,
            ),
        ),
        (
            "facet hits",
            lambda: cli._emit_facet_hits(
                [
                    {
                        "project": "p",
                        "score": 0.5,
                        "title": "T",
                        "status": "active",
                        "relpath": "t.md",
                        "line_start": 1,
                        "line_end": 4,
                        "deps": 1,
                        "dependents": 0,
                        "tainted": True,
                        "snippet": "body",
                        "store": "design",
                    }
                ],
                _DESIGN_ARGS,
            ),
        ),
        (
            "note hits",
            lambda: cli._emit_human(
                [
                    {
                        "project": "p",
                        "relpath": "n.md",
                        "heading": "H",
                        "title": "N",
                        "snippet": "s",
                        "score": 0.9,
                        "line_start": 2,
                        "line_end": 3,
                        "tainted": True,
                        "index_rebuilding": True,
                        "store": "notes",
                    }
                ]
            ),
        ),
        (
            "lookup all",
            lambda: cli._emit_lookup_all(
                [
                    {
                        "score": 0.4,
                        "kind": "plan",
                        "title": "T",
                        "status": "todo",
                        "relpath": "t.md",
                        "heading": "H",
                        "project": "p",
                        "store": "plans",
                    }
                ],
                False,
            ),
        ),
        ("code lookup", lambda: cli._emit_code([_SYM], "code-lookup", False)),
        ("code xref", lambda: cli._emit_code([_SYM], "code-xref", False)),
        (
            "code dossier",
            lambda: cli._emit_code_dossier(
                {
                    **_SYM,
                    "signature": "def f():",
                    "calls": [{"symbol": "a.g", "description": "g"}],
                },
                False,
            ),
        ),
        (
            "learning read",
            lambda: cli._emit_code_learning(
                {
                    "symbol": "a.f",
                    "relpath": "a.f.md",
                    "found": True,
                    "body": "b",
                    "project": "p",
                },
                "learning-read",
                False,
            ),
        ),
        (
            "learning report",
            lambda: cli._emit_code_report(
                [
                    {"status": "ok", "symbol": "a.f"},
                    {"status": "orphan", "symbol": "a.g", "file": "a.py"},
                ],
                False,
            ),
        ),
        (
            "status",
            lambda: cli._emit_status(
                {
                    "store": "S",
                    "embed_model": "E",
                    "git": {"enabled": True, "remote": "git@x:y.git", "dirty": 3,
                            "ahead": 1, "behind": 0, "last_commit": "abc msg"},
                    "lsp_sessions": [
                        {"server": "ty", "root": "/r", "pid": 1, "alive": False},
                        {"server": "gopls", "root": "/g", "pid": 2, "alive": True,
                         "busy": True},
                    ],
                    "reconciling": True,
                    "reconcile_remaining": 2,
                    "sweeps": {"p": {"done": 1, "total": 3}},
                    "daemon": {"uptime_s": 90, "log_file": "/tmp/d.log",
                               "background": [{"job": "j", "running_s": 5,
                                               "detail": "x"}],
                               "queues": {"describe": 4},
                               "watchers": {"notes": {"state": "delayed",
                                                      "last_latency_s": 3}}},
                    "generation": {"mode": "on",
                                   "pending_descriptions": {"p": 7}},
                    "projects": [
                        {
                            "project": "p",
                            "notes": 1,
                            "doc_chunks": 0,
                            "symbols": 2,
                            "learnings": 0,
                            "design_tainted": 1,
                        }
                    ],
                },
                False,
            ),
        ),
    ],
)
def test_styling_adds_escape_codes_and_nothing_else(name, emit, tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    plain, styled = _both(emit)
    assert plain and "\x1b" not in plain
    assert "\x1b[" in styled, name
    assert _plain(styled) == plain, name


def test_an_item_path_links_to_its_file_under_the_configured_root(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    from crib.config import Config
    from crib.paths import Paths, resolve_project_paths

    paths = Paths.resolve().ensure()
    f = resolve_project_paths(paths, Config(), "p").pillar_dir("plans") / "t.md"
    f.parent.mkdir(parents=True)
    f.write_text("# T\n")
    style.configure("always")
    assert cli._item_link("p", "t.md", "plans") == f.as_uri()
    assert cli._item_link("p", "missing.md", "plans") is None
    assert cli._item_link("p", "t.md", "design") is None  # another facet's dir
    style.configure("never")
    assert cli._item_link("p", "t.md", "plans") is None  # no link while unstyled


_NODE = {"fqn": "a.f", "kind": "function", "file": "a.py", "line": 3, "project": "p"}
_SECOND_PASS = [
    ("write result", lambda: cli._emit_write_result(
        {"project": "p", "relpath": "n.md", "project_created": True,
         "similar": [{"score": 0.9, "relpath": "m.md", "heading": "H"}]})),
    ("move result", lambda: cli._emit_write_result(
        {"from": {"project": "p", "relpath": "a.md"},
         "to": {"project": "q", "relpath": "a.md"}})),
    ("design write", lambda: cli._emit_design_write(
        {"project": "p", "relpath": "t.md", "title": "T", "status": "active",
         "deps": ["d"], "warnings": ["w"],
         "newly_tainted": [{"title": "U", "relpath": "u.md", "via": ["x"]}],
         "unblocked": [{"title": "V", "ref": "v.md"}],
         "similar": [{"score": 0.8, "relpath": "s.md"}], "missing": ["m"],
         "next": "n"}, _DESIGN_ARGS)),
    ("design forget", lambda: cli._emit_design_write(
        {"project": "p", "relpath": "t.md", "removed": True,
         "dependents": [{"title": "U", "relpath": "u.md"}]}, _DESIGN_ARGS)),
    ("plan batch", lambda: cli._emit_design_write(
        {"project": "p", "added": 2, "items": [{"title": "A", "relpath": "a.md"},
                                               {"title": "B", "relpath": "b.md"}]},
        _PLAN_ARGS)),
    ("design tree", lambda: cli._emit_design_tree(
        {"project": "p", "direction": "deps", "roots": [
            {"title": "R", "relpath": "r.md", "status": "active", "children": [
                {"title": "C", "relpath": "c.md", "status": "proposed", "tainted": True},
                {"title": "X", "missing": True}]}]},
        SimpleNamespace(cmd="design", json=False, ascii=False))),
    ("design check", lambda: cli._emit_design_check(
        {"project": "p", "designs": 3, "cycles": [["a", "b"]],
         "proposed": [{"title": "P", "relpath": "p.md", "next": "promote"}],
         "tainted": [{"title": "T", "relpath": "t.md", "next": "read",
                      "causes": [{"change_kind": "edit", "reason": "r"}],
                      "paths": [{"chain": ["a", "b"]}]}]}, _DESIGN_ARGS)),
    ("design import", lambda: cli._emit_design_import(
        {"relpath": "DESIGN.md", "path": "/tmp/DESIGN.md", "instruction": "do",
         "existing": [{"title": "E", "relpath": "e.md", "cites": ["DESIGN.md#1"]}],
         "sections": [{"words": 12, "source": "DESIGN.md#1", "preview": "p"}]},
        _DESIGN_ARGS)),
    ("code graph", lambda: cli._emit_code_graph(
        {**_NODE, "resolved": {"query": "f", "fqn": "a.f", "via": "name"},
         "children": [{**_NODE, "fqn": "a.g", "repeat": True}]},
        SimpleNamespace(json=False, ascii=False, callers=False, references=False))),
    ("code edges", lambda: cli._emit_code_edges(
        {"root": "a.f", "direction": "callees", "depth": 2,
         "nodes": [{**_NODE, "depth": 0}, {**_NODE, "fqn": "a.g", "depth": 1}],
         "edges": [{"from": "a.f", "to": "a.g"}, {"from": "a.h", "to": "a.g"}]},
        argparse.Namespace(ascii=False))),
    ("rehome", lambda: cli._emit_code_rehome(
        {"old": "a.f", "candidates": [{**_NODE, "score": 0.7}]}, False)),
    ("projects", lambda: cli._emit_projects(
        [{"project": "p", "store_root": "$DEV/p", "unavailable": True}], False)),
    ("project status", lambda: cli._emit_project(
        {"project": "p", "indexed": False, "symbols": 1, "files": 1,
         "kinds": {"function": 1}}, "status", False)),
    ("project index", lambda: cli._emit_project(
        {"project": "p", "files_indexed": 1, "files_seen": 2,
         "errors": [{"file": "x.py"}],
         "empty_extracts": [{"file": "y.ts", "server": "tsgo"}],
         "facets": {"routine": {"dir": "w", "glob": "*.md", "items": 3,
                                "read_only": True}}}, "index", False)),
    ("ops", lambda: cli._emit_ops(
        {"sweeps": {}, "generation": {"inflight": 1}, "indexing": {"p": ["a"]}},
        False)),
    ("doctor", lambda: cli._emit_doctor(
        {"project": "p", "counts": {"stale": 1},
         "findings": [{"check": "stale", "item": "t.md", "fix": "read it",
                       "similarity": 0.9}]}, False)),
    ("related", lambda: cli._emit_related(
        {"item": {"kind": "plan", "title": "T", "status": "todo", "relpath": "t.md",
                  "project": "p", "store": "plans"},
         "links_out": [{"link": "rests_on", "kind": "design", "title": "D"}],
         "links_in": [],
         "similar": {"note": [{"similarity": 0.5, "kind": "note", "title": "N"}]}},
        False)),
]


@pytest.mark.parametrize("name, emit", _SECOND_PASS)
def test_second_pass_styling_adds_escape_codes_and_nothing_else(
        name, emit, tmp_path, monkeypatch):
    test_styling_adds_escape_codes_and_nothing_else(name, emit, tmp_path, monkeypatch)
