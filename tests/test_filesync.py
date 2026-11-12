"""crib.filesync: which of a facet's files need mapping again."""

from __future__ import annotations

import os

from crib.filesync import changes
from crib.items import Item
from crib.store import InMemoryStore


def _item(i: str) -> Item:
    return Item(i, "symbol", "p", "x.py", i, "h")


def test_new_changed_and_removed_files_and_nothing_else(tmp_path):
    s = InMemoryStore()
    (tmp_path / "a.toml").write_text("a")
    (tmp_path / "b.toml").write_text("b")
    (tmp_path / ".dot.toml").write_text("d")  # slugs may start with a dot
    (tmp_path / "skip.md").write_text("x")  # not this facet's suffix
    ch = changes(s, "symbol", "p", tmp_path, ".toml")
    assert set(ch.changed) == {"a.toml", "b.toml", ".dot.toml"} and ch.removed == []
    s.replace_files(
        "symbol", "p", [(n, m, z, [_item(n)]) for n, (m, z) in ch.changed.items()], []
    )
    assert not changes(s, "symbol", "p", tmp_path, ".toml")  # all current

    (tmp_path / "a.toml").write_text("a, longer")
    st = (tmp_path / "a.toml").stat()
    os.utime(tmp_path / "a.toml", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    (tmp_path / "b.toml").unlink()
    ch = changes(s, "symbol", "p", tmp_path, ".toml")
    assert list(ch.changed) == ["a.toml"] and ch.removed == ["b.toml"]


def test_only_limits_the_diff_to_the_named_files(tmp_path):
    s = InMemoryStore()
    (tmp_path / "a.toml").write_text("a")
    (tmp_path / "b.toml").write_text("b")
    ch = changes(s, "symbol", "p", tmp_path, ".toml", only=["a.toml", "gone.toml"])
    assert list(ch.changed) == ["a.toml"] and ch.removed == []
    s.replace_files("symbol", "p", [("gone.toml", 1, 1, [_item("g")])], [])
    ch = changes(s, "symbol", "p", tmp_path, ".toml", only=["gone.toml"])
    assert ch.removed == ["gone.toml"] and not ch.changed


def test_a_missing_directory_removes_everything_it_had(tmp_path):
    s = InMemoryStore()
    s.replace_files("symbol", "p", [("a.toml", 1, 1, [_item("a")])], [])
    ch = changes(s, "symbol", "p", tmp_path / "nope", ".toml")
    assert ch.removed == ["a.toml"]
