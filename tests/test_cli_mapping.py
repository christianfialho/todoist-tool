"""Tests for the thin CLI's argparse surface -> run_command arg mapping.

The CLI is a thin wrapper: its only job is to translate an ``argparse``
spelling into the keyword args that ``core.run_command`` understands, and to
normalize subcommand aliases (``list``/``projects``, ``bucket-create``, etc.)
back to the canonical command name. These tests stub ``core.run_command`` and
confirm the mapping, so a dest-rename or alias-drift in cli.py fails loudly
here instead of silently doing the wrong thing against the real account.
"""

from __future__ import annotations

import todoist.cli as cli_mod


def _capture(monkeypatch):
    """Call CLI main() with stubbed run_command; return (cmd, kwargs)."""
    calls = {}

    def fake_run(command, **kw):
        calls["cmd"] = command
        calls["kw"] = kw
        return {"ok": True, "cmd": command, "output": "ok\n"}

    monkeypatch.setattr(cli_mod.core, "run_command", fake_run)
    return calls


def test_list_and_projects_alias_to_canonical(monkeypatch):
    for spelling in ("list", "projects"):
        calls = _capture(monkeypatch)
        assert cli_mod.main([spelling]) == 0
        assert calls["cmd"] == "list", spelling


def test_bucket_create_alias_normalized(monkeypatch):
    calls = _capture(monkeypatch)
    assert cli_mod.main(["create", "--name", "New Bucket"]) == 0
    assert calls["cmd"] == "create"
    assert calls["kw"]["name"] == "New Bucket"


def test_bucket_create_alias_spelling(monkeypatch):
    calls = _capture(monkeypatch)
    assert cli_mod.main(["bucket-create", "--name", "X"]) == 0
    assert calls["cmd"] == "create"


def test_rename_passes_bucket_and_name(monkeypatch):
    calls = _capture(monkeypatch)
    assert cli_mod.main(["rename", "--bucket", "Work", "--name", "Backend"]) == 0
    assert calls["cmd"] == "rename"
    assert calls["kw"]["bucket"] == "Work"
    assert calls["kw"]["name"] == "Backend"


def test_rename_project_alias_sets_bucket_dest(monkeypatch):
    # Hidden --project alias must land in the same `bucket` dest as --bucket.
    calls = _capture(monkeypatch)
    assert cli_mod.main(["rename", "--project", "Work", "--name", "Backend"]) == 0
    assert calls["cmd"] == "rename"
    assert calls["kw"]["bucket"] == "Work"
    assert "project" not in calls["kw"]


def test_release_and_aliases(monkeypatch):
    for spelling in ("release", "bucket-release"):
        calls = _capture(monkeypatch)
        assert cli_mod.main([spelling, "Work"]) == 0
        assert calls["cmd"] == "release"
        assert calls["kw"]["name"] == "Work"


def test_status_and_bucket_status_alias(monkeypatch):
    for spelling in ("status", "bucket-status"):
        calls = _capture(monkeypatch)
        assert cli_mod.main([spelling]) == 0
        assert calls["cmd"] == "status"


def test_add_bucket_alias_maps_through(monkeypatch):
    calls = _capture(monkeypatch)
    assert cli_mod.main(["add", "--content", "hello", "--bucket", "Work"]) == 0
    assert calls["cmd"] == "add"
    assert calls["kw"]["bucket"] == "Work"


def test_move_requires_bucket(monkeypatch):
    calls = _capture(monkeypatch)
    assert cli_mod.main(["move", "--task", "t1", "--project", "Work", "--section", "S"]) == 0
    assert calls["cmd"] == "move"
    assert calls["kw"]["bucket"] == "Work"