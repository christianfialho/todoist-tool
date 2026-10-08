"""Safety + reversibility tests for the bucket commands.

The single most important invariant of the bucket design (see core.py:66-70):

    Bucket commands NEVER create, delete, or archive a project object. They
    only RENAME a project between a "Spare Project N" marker and a live
    outcome name. The Todoist object counts (projects = slots) therefore
    never change -- which is the entire point: it preserves the account's
    grandfathered-in extended project limit.

These tests prove that invariant by stubbing the network layer and driving
the REAL ``core.run_command`` dispatcher (the same path the MCP server and
CLI use). Any emission of a project CREATE / DELETE / ARCHIVE fails the test.

Also covered: release is reversible -- ``undo`` reopens every task that
release closed and renames the bucket back, without burning a slot.
"""

from __future__ import annotations

import todoist.core as core


# --------------------------------------------------------------------------
# Fixture: a snapshot with an inbox, two spares, and one live bucket.
# --------------------------------------------------------------------------
def _fixture():
    return {
        "projects": [
            {"id": "p-inbox", "name": "Inbox", "inbox_project": True},
            {"id": "p-live", "name": "Work", "inbox_project": False},
            {"id": "p-sp1", "name": "Spare Project 1", "inbox_project": False},
            {"id": "p-sp2", "name": "Spare Project 2", "inbox_project": False},
        ],
        "sections": [],
        "labels": [],
        "user": {},
        "notes": [],
        "project_notes": [],
        "items": [
            # two open tasks in the live bucket (for release + undo)
            {"id": "t1", "project_id": "p-live", "content": "ship api",
             "checked": False},
            {"id": "t2", "project_id": "p-live", "content": "write docs",
             "checked": False},
            # one open task inside a spare -> proves create refuses non-empty spare
            {"id": "t3", "project_id": "p-sp2", "content": "leftover note",
             "checked": False},
            # one completed task in the live bucket -> release must NOT close it
            {"id": "t4", "project_id": "p-live", "content": "done already",
             "checked": True},
        ],
    }


# --------------------------------------------------------------------------
# Test stubs
# --------------------------------------------------------------------------
class FakeHttp:
    """Records every _http call; never touches the real network."""

    def __init__(self, monkeypatch, fixture):
        self.calls = []  # (method, path, body)
        self.ops = []    # ops captured via _log_op
        self.read_ops = []  # what undo sees (_read_ops)

        monkeypatch.setattr(core, "_sync", lambda: fixture)
        monkeypatch.setattr(
            core, "_http",
            lambda method, path, body=None: self.calls.append((method, path, body)) or {})
        monkeypatch.setattr(core, "_log_op",
                            lambda rec: self.ops.append(
                                rec | {"op_id": f"op-{len(self.ops) + 1:03d}"}))
        monkeypatch.setattr(core, "_read_ops", lambda: self.read_ops)
        monkeypatch.setattr(core, "load_token", lambda: "TEST-TOKEN")


FORBIDDEN = (
    "bucket commands must never emit {desc} -- slot count is immutable."
)


def _forbidden(calls):
    """Return a list of forbidden emissions found in the recorded calls."""
    bad = []
    for method, path, body in calls:
        desc = None
        # delete a project object outright
        if method == "DELETE" and path.startswith("/projects/"):
            desc = f"DELETE {path}"
        # create a project object (bare /projects with no id)
        elif method == "POST" and path == "/projects":
            desc = f"POST /projects (project CREATE)"
        # archive a project (is_archived: true)
        elif isinstance(body, dict) and body.get("is_archived") is True:
            desc = f"{method} {path} archive"
        if desc:
            bad.append((method, path, body, desc))
    return bad


def _assert_slot_safe(fake):
    bad = _forbidden(fake.calls)
    assert not bad, "\n".join(
        FORBIDDEN.format(desc=d) + f"\n  actual: {m} {p} {b}"
        for m, p, b, d in bad)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------
def test_create_is_a_rename_not_a_create(monkeypatch):
    fake = FakeHttp(monkeypatch, _fixture())
    res = core.run_command("create", name="New Bucket")
    assert res["ok"], res
    assert "created" in res["output"]

    # The lowest-numbered EMPTY spare (Spare Project 1) was reused.
    renames = [(m, p, b) for m, p, b in fake.calls
               if m == "POST" and p == "/projects/p-sp1"]
    assert renames, "expected a rename of the spare project object"
    _, _, body = renames[0]
    assert body["name"] == "New Bucket"
    assert body.get("is_archived") is False  # never archive
    assert core.SPARE_RE.match(body["name"]) is None

    # The other spare (p-sp2) must be untouched, and no project created/deleted.
    assert not any(p == "/projects/p-sp2" for m, p, b in fake.calls if m == "POST")
    _assert_slot_safe(fake)
    assert fake.ops[-1]["action"] == "bucket-create"


def test_create_refuses_nonempty_spare(monkeypatch):
    fake = FakeHttp(monkeypatch, _fixture())
    # Force a state where the ONLY spare (rename all but one away) holds an
    # open task -> create must fail without touching the network write path.
    res = core.run_command("create", name="New Bucket")
    assert res["ok"]  # sanity: baseline works
    # Simulate exhaustion: make Spare Project 1 also hold an open task.
    fx = _fixture()
    fx["items"].append({"id": "t5", "project_id": "p-sp1", "content": "blocker",
                        "checked": False})
    fake2 = FakeHttp(monkeypatch, fx)
    res = core.run_command("create", name="Another")
    assert not res["ok"]
    assert "no spare buckets available" in res["error"] or "still holds" in res["error"]
    # Even on failure, nothing was emitted.
    _assert_slot_safe(fake2)


def test_rename_only_renames(monkeypatch):
    fake = FakeHttp(monkeypatch, _fixture())
    res = core.run_command("rename", bucket="Work", name="Backend")
    assert res["ok"], res
    post = [(m, p, b) for m, p, b in fake.calls if m == "POST"]
    assert len(post) == 1
    method, path, body = post[0]
    assert path == "/projects/p-live"
    assert body["name"] == "Backend"
    _assert_slot_safe(fake)
    assert fake.ops[-1]["action"] == "bucket-rename"


def test_project_alias_still_works(monkeypatch):
    # Hidden `project` alias accepted by the arg resolver, same as `bucket`.
    fake = FakeHttp(monkeypatch, _fixture())
    res = core.run_command("rename", project="Work", name="Backend")
    assert res["ok"], res
    _assert_slot_safe(fake)


def test_release_closes_tasks_and_renames_to_spare(monkeypatch):
    fake = FakeHttp(monkeypatch, _fixture())
    res = core.run_command("release", name="Work")
    assert res["ok"], res
    assert "released" in res["output"]
    assert "tasks_closed" in res["output"]

    # Only the OPEN tasks (t1, t2) of the live bucket were closed; the already
    # completed one (t4) was not touched, and the spare's task (t3) was not.
    closes = [(m, p) for m, p, b in fake.calls if m == "POST" and "/close" in p]
    assert sorted(p for m, p in closes) == ["/tasks/t1/close", "/tasks/t2/close"]

    # The bucket was renamed to the next free spare number (3, since 1&2 used).
    proj_posts = [(m, p, b) for m, p, b in fake.calls
                  if m == "POST" and p == "/projects/p-live"]
    assert len(proj_posts) == 1
    _, _, body = proj_posts[0]
    assert body["name"] == "Spare Project 3"
    assert body.get("is_archived") is False

    _assert_slot_safe(fake)
    rec = fake.ops[-1]
    assert rec["action"] == "bucket-release"
    assert {t["id"] for t in rec["closed_tasks"]} == {"t1", "t2"}


def test_undo_release_reopens_and_renames_back(monkeypatch):
    fake = FakeHttp(monkeypatch, _fixture())
    res = core.run_command("release", name="Work")
    assert res["ok"], res
    rec = fake.ops[-1]
    assert rec["action"] == "bucket-release"

    # undo reads the op log; give it back the release record we just captured.
    fake.read_ops = [rec]
    res = core.run_command("undo", op=rec["op_id"])
    assert res["ok"], res
    assert "undone" in res["output"]

    # Every task release closed was reopened; the already-completed one was
    # not reopened (it was never in closed_tasks).
    reopens = [p for m, p, b in fake.calls if m == "POST" and "/reopen" in p]
    assert sorted(reopens) == ["/tasks/t1/reopen", "/tasks/t2/reopen"]

    # The bucket was renamed back to its original live name.
    last_proj_post = next(
        (m, p, b) for m, p, b in reversed(fake.calls)
        if m == "POST" and p == "/projects/p-live")
    _, _, undo_body = last_proj_post
    assert undo_body["name"] == "Work"
    assert undo_body.get("is_archived") is False
    _assert_slot_safe(fake)


def test_status_and_list_are_read_only(monkeypatch):
    fake = FakeHttp(monkeypatch, _fixture())
    res = core.run_command("status")
    assert res["ok"], res
    assert "live_count" in res["output"] and "spare_count" in res["output"]

    res = core.run_command("list")
    assert res["ok"], res

    # read-only commands must emit ZERO network calls
    _assert_slot_safe(fake)
    assert fake.calls == [], f"status/list should not call the API: {fake.calls}"
    assert fake.ops == []