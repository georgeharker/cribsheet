"""Repo-scoped ops must resolve their project from `project_path`'s `.crib`, not
the sticky session project. Regression: a project_index(project_path=/other/repo)
with a sticky current project once indexed the OTHER repo INTO the current one."""

from __future__ import annotations

import pytest

from crib.app import Crib
from crib.config import Config
from crib.paths import Paths
from crib.server import _source_project
from crib.session import session_state
from crib.store import InMemoryStore


@pytest.fixture()
def crib(tmp_path, monkeypatch):
    monkeypatch.setenv("CRIB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("CRIB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CRIB_INDEX_DIR", str(tmp_path / "index"))
    return Crib(Paths.resolve().ensure(), Config(), InMemoryStore())


def test_explicit_project_wins(crib):
    assert _source_project(crib, "chosen", "/some/repo") == "chosen"


def test_project_path_defers_to_crib_not_sticky(crib):
    # sticky session is on some project…
    session_state().current_project = "cribsheet"
    # …but a repo-scoped call names a DIFFERENT repo via project_path → return None
    # so crib.project_* reads link.project from THAT repo's .crib (never the sticky).
    assert _source_project(crib, None, "/Users/me/other-repo") is None


def test_no_path_falls_back_to_session(crib):
    session_state().current_project = "sticky-proj"
    assert _source_project(crib, None, None) == "sticky-proj"
