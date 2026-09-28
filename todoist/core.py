#!/usr/bin/env python3
"""Todoist core — the full Todoist integration layer (MCP refactor).

Everything that used to live in ``~/.hermes/profiles/christian/scripts/
todoist_layer.py`` was lifted here verbatim (v1 API base, sync snapshot,
hierarchy/render/filter logic, all ``cmd_*`` handlers, the JSONL op/undo log,
report plugins, the argparse surface). Two deliberate seams were added:

  * ``ToolError`` replaces ``sys.exit(...)`` so errors propagate cleanly
    instead of killing the process (an MCP server must never exit).
  * A ``State`` mediation object (rate-limit gate with a small disk backstop +
    short-TTL snapshot sync cache) wraps the low-level ``_http``/``_sync``
    calls, giving a long-lived process real memory across invocations.

``run_command(command, **kwargs)`` is the single dispatcher both front-ends
use: the thin CLI (``cli.py`` / the historical shim) and the MCP stdio server
(``todoist_mcp/server.py``).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from types import SimpleNamespace as _NS


# ── Configuration ───────────────────────────────────────────────────────────
ENV_PATH = "/home/fialho/.hermes/profiles/christian/.env"
TOKEN_VARS = ("TODOIST_API_KEY", "TODOIST_API_TOKEN")
BASE = "https://api.todoist.com/api/v1"
SYNC_RESOURCES = [
    "projects", "sections", "items", "notes", "project_notes",
    "labels", "user",
]
_CHRISTIAN_SCRIPTS = os.path.dirname(ENV_PATH)
REPORTS_DIR = os.path.join(
    os.environ.get("TODOIST_REPORTS_DIR", _CHRISTIAN_SCRIPTS), "todoist_reports")

# Priority: in the v1 API 4 = P1 (very urgent) and 1 = P4 (natural); clients
# display P1..P4.
PRIORITY_NAME = {4: "P1", 3: "P2", 2: "P3", 1: "P4"}
PRIORITY_READABLE = {
    4: "P1 very urgent", 3: "P2 high", 2: "P3 medium", 1: "P4 none",
}

# ── mediation config (env-overridable) ──────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_FILE = os.environ.get(
    "TODOIST_RATELIMIT_STATE",
    os.path.join(_HERE, ".ratelimit.json"))
DEFAULT_SYNC_TTL = float(os.environ.get("TODOIST_SYNC_TTL", "5.0"))

# The historic op log / reports stayed at the christian scripts path.
OP_LOG = os.path.join(os.environ.get("TODOIST_OP_LOG",
    os.path.dirname(ENV_PATH)), "todoist_ops.jsonl")


# ── errors ──────────────────────────────────────────────────────────────────
class ToolError(Exception):
    """A clean, user-facing tool error (replaces the original sys.exit)."""


class RateLimited(Exception):
    """Raised while the rate-limit gate is cooling down."""
    def __init__(self, message, retry_after=0.0, strikes=0, hard_stop=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.strikes = strikes
        self.hard_stop = hard_stop


# ── state: rate-limit gate + sync cache ─────────────────────────────────────
class RateGate:
    """In-memory + small-disk-copy rate-limit gate.

    On a 429 we back off ``until = now + retry_after`` (escalating: the 3rd
    strike within a window flushes to a hard stop of >= 10 minutes). Every
    network success resets the strike count. ``check()`` raises RateLimited
    while cooling down. A tiny JSON backstop file survives /reload-mcp and
    crashes; it is transient and never holds secrets.
    """

    MAX_STRIKES = 3
    HARD_STOP_MIN = 600.0
    MULTIPLIER = 4.0

    def __init__(self, state_file=DEFAULT_STATE_FILE):
        self.state_file = state_file
        self.until = 0.0
        self.strikes = 0
        self._load()

    def _load(self):
        try:
            with open(self.state_file) as fh:
                d = json.load(fh)
            self.until = float(d.get("until", 0.0))
            self.strikes = int(d.get("strikes", 0))
        except Exception:
            self.until = 0.0
            self.strikes = 0

    def _save(self):
        try:
            with open(self.state_file, "w") as fh:
                json.dump({"until": self.until, "strikes": self.strikes,
                           "updated": time.time()}, fh)
        except OSError:
            pass  # backstop is best-effort; never fatal

    def remaining(self):
        return max(0.0, self.until - time.time())

    def check(self):
        left = self.remaining()
        if left > 0:
            raise RateLimited(
                f"rate-limited: retry in {int(left)}s (strike {self.strikes or 1}/"
                f"{self.MAX_STRIKES})", retry_after=left,
                strikes=self.strikes or 1)

    def record_429(self, wait):
        """Note a fresh 429 from the API. wait = server's retry_after (s)."""
        self.strikes += 1
        if self.strikes >= self.MAX_STRIKES:
            wait = max(wait, self.HARD_STOP_MIN)
            hard_stop = True
        else:
            hard_stop = False
        self.until = time.time() + wait
        self._save()
        raise RateLimited(
            f"rate-limited: retry in {int(wait)}s (strike {self.strikes}/"
            f"{self.MAX_STRIKES})" + (" — hard stop after 3 strikes" if hard_stop else ""),
            retry_after=wait, strikes=self.strikes, hard_stop=hard_stop)

    def reset(self):
        if self.remaining() > 0 or self.strikes:
            self.until = 0.0
            self.strikes = 0
            self._save()


class SyncCache:
    """Short-TTL snapshot cache. Invalidate() is called on any write."""

    def __init__(self, ttl=DEFAULT_SYNC_TTL):
        self.ttl = ttl
        self._data = None
        self._ts = 0.0

    def get(self):
        if self._data is not None and (time.time() - self._ts) < self.ttl:
            return self._data
        return None

    def put(self, data):
        self._data = data
        self._ts = time.time()

    def invalidate(self):
        self._data = None
        self._ts = 0.0


class State:
    """Per-process shared state for the long-lived server."""

    def __init__(self, state_file=DEFAULT_STATE_FILE):
        self.gate = RateGate(state_file=state_file)
        self.cache = SyncCache(ttl=DEFAULT_SYNC_TTL)


_state = State()


# ── Low-level HTTP (with mediation) ─────────────────────────────────────────
def load_token():
    if not os.path.exists(ENV_PATH):
        raise ToolError(f"error: no env file {ENV_PATH}")
    with open(ENV_PATH) as fh:
        for line in fh:
            for var in TOKEN_VARS:
                if line.startswith(var + "="):
                    val = line.split("=", 1)[1].strip().strip("\"'")
                    if val:
                        return val
    raise ToolError("error: no TODOIST_API_KEY/TODOIST_API_TOKEN in " + ENV_PATH)


def _http(method, path, body=None):
    """Single authenticated call to the Todoist v1 API, mediated by State."""
    _state.gate.check()  # raises RateLimited while cooling down
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("Authorization", "Bearer " + load_token())
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode("utf-8")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            _state.gate.reset()  # any success resets the strike count
            # any write (non-GET, non-sync) busts the snapshot cache
            if method != "GET" and path != "/sync":
                _state.cache.invalidate()
            return data
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = " :: " + e.read().decode("utf-8", "replace")
        except Exception:
            pass
        if e.code == 429:
            wait = _retry_after(e, body)
            # record_429 raises RateLimited
            _state.gate.record_429(wait)
        raise ToolError(f"error: HTTP {e.code} {method} {path}{detail}")


def _retry_after(http_err, body):
    """Best-effort retry-after for a 429: body.error_extra.retry_after, then
    the Retry-After header, then the caller's wait hint, then ~60s."""
    try:
        detail = http_err.read().decode("utf-8", "replace")
        parsed = json.loads(detail) if detail.startswith("{") else {}
        ra = parsed.get("error_extra", {}).get("retry_after")
        if ra:
            return float(ra)
    except Exception:
        pass
    try:
        h = http_err.headers.get("Retry-After")
        if h:
            return float(h)
    except Exception:
        pass
    return 60.0


def _sync():
    """POST /sync with snapshot-resource_types, served from the short-TTL
    cache when not stale."""
    cached = _state.cache.get()
    if cached is not None:
        return cached
    data = _http("POST", "/sync", {
        "sync_token": "*",
        "resource_types": SYNC_RESOURCES,
    })
    _state.cache.put(data)
    return data


def _probe_task(pid):
    """Return True if a task with this id exists server-side, without raising.
    Needed for write ops on tasks that completed and dropped out of the
    active-item snapshot (reopen/move/delete for finished tasks)."""
    req = urllib.request.Request(BASE + f"/tasks/{pid}", method="GET")
    req.add_header("Authorization", "Bearer " + load_token())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return bool(resp.read())
    except urllib.error.HTTPError:
        return False


# ── operation log (audit + lossless undo) ───────────────────────────────────
def _next_op_id():
    """Return the next monotonic, human-friendly op id like 'op-003'."""
    n = 0
    if os.path.exists(OP_LOG):
        try:
            for line in open(OP_LOG):
                line = line.strip()
                if not line:
                    continue
                m = json.loads(line).get("op_id", "")
                if m.startswith("op-") and m[3:].isdigit():
                    n = max(n, int(m[3:]))
        except Exception:
            pass
    return f"op-{n + 1:03d}"


def _log_op(rec):
    """Silently append one JSON line to the op log (audit + undo). Never fatal."""
    rec.setdefault("op_id", _next_op_id())
    rec.setdefault("ts", _dt.datetime.now().isoformat(timespec="seconds"))
    try:
        with open(OP_LOG, "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


def _read_ops():
    out = []
    if os.path.exists(OP_LOG):
        for line in open(OP_LOG):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _task_prev(snap, tid):
    """Capture the editable fields of a task, for lossless undo of updates."""
    t = snap.items.get(tid, {})
    return {
        "content": t.get("content"),
        "description": t.get("description"),
        "priority": t.get("priority"),
        "due": t.get("due"),
        "deadline": t.get("deadline"),
        "labels": snap.label_names(t) if t.get("id") else [],
        "project_id": t.get("project_id"),
        "section_id": t.get("section_id"),
        "parent_id": t.get("parent_id"),
    }


# ── Snapshot: the whole account from ONE sync call ──────────────────────────
class Todoist:
    """A point-in-time view of the entire Todoist account."""

    def __init__(self, fresh=True):
        if fresh:
            data = _sync()
        else:
            with open("/tmp/t_sync2.json") as fh:
                data = json.load(fh)
        self.raw = data
        self.projects = {p["id"]: p for p in data.get("projects", [])}
        self.sections = {s["id"]: s for s in data.get("sections", [])}
        self.labels = {l["id"]: l for l in data.get("labels", [])}
        self.items = {t["id"]: t for t in data.get("items", [])}
        self.user = data.get("user") or {}

        # task comments by item_id (sorted oldest->newest)
        self.comments = {}
        for n in data.get("notes", []):
            if not n.get("is_deleted"):
                self.comments.setdefault(n.get("item_id"), []).append(n)
        for lst in self.comments.values():
            lst.sort(key=lambda n: n.get("posted_at") or "")
        # project-level comments
        self.project_comments = {}
        for n in data.get("project_notes", []):
            if not n.get("is_deleted"):
                self.project_comments.setdefault(n.get("project_id"), []).append(n)

        # comment_id -> {kind: 'task'|'project', owner: id, note: {...}} ,
        # for delete-comment / undo. 'owner' is item_id for task comments,
        # project_id for project comments.
        self.comments_by_id = {}
        for n in data.get("notes", []):
            if not n.get("is_deleted"):
                self.comments_by_id[n["id"]] = {
                    "kind": "task", "owner": n.get("item_id"), "note": n}
        for n in data.get("project_notes", []):
            if not n.get("is_deleted"):
                self.comments_by_id[n["id"]] = {
                    "kind": "project", "owner": n.get("project_id"), "note": n}

        # name -> id index (first-name, case-insensitive, ignoring deleted)
        self._by_name = {}
        for pid, p in self.projects.items():
            if not p.get("is_deleted"):
                self._by_name.setdefault(p["name"].strip().lower(), pid)

    # ── small accessors ──────────────────────────────────────────────────────
    def label_names(self, item):
        """Task labels -> friendly names. The v1 sync API returns label NAMES
        (not ids); tolerate raw ids too by mapping through the label index when
        the entry matches one."""
        out = []
        for entry in item.get("labels", []):
            if entry in self.labels:
                out.append(self.labels[entry]["name"])
            else:
                out.append(str(entry))
        return sorted(out)

    def project_name(self, pid):
        p = self.projects.get(pid)
        return p["name"] if p else "?"

    def section_name(self, sid):
        s = self.sections.get(sid)
        return s["name"] if s else None

    def task_comments(self, item):
        return self.comments.get(item.get("id"), [])

    def due_obj(self, item):
        """Compact 'when' summary combining due (soft) + deadline (hard)."""
        parts = []
        due = item.get("due")
        if due and due.get("date"):
            s = due.get("string") or due.get("date")
            if due.get("is_recurring"):
                s += " (recurring)"
            parts.append("due " + s)
        dl = item.get("deadline")
        if isinstance(dl, dict) and dl.get("date"):
            parts.append("deadline " + dl["date"])
        return "; ".join(parts) if parts else None

    def priority_name(self, item):
        return PRIORITY_NAME.get(item.get("priority", 1), "P4")

    def is_open(self, item):
        return not item.get("checked") and not item.get("is_deleted")

    # ── name/id resolution ───────────────────────────────────────────────────
    def resolve_project(self, query):
        if not query:
            return None
        query = str(query).strip()
        if query in self.projects:
            return query
        low = query.lower()
        if low in self._by_name:
            return self._by_name[low]
        cands = [pid for pid, p in self.projects.items()
                 if not p.get("is_deleted") and low in p["name"].lower()]
        if len(cands) == 1:
            return cands[0]
        if len(cands) > 1:
            raise ValueError(
                "ambiguous project %r -> %s" % (query,
                                                [self.projects[c]["name"] for c in cands]))
        raise ValueError("unknown project %r. Known: %s" % (
            query, sorted(p["name"] for p in self.projects.values()
                          if not p.get("is_deleted"))))

    def resolve_section(self, project_id, query):
        if not query:
            return None
        query = str(query).strip()
        low = query.lower()
        for sid, s in self.sections.items():
            if (s.get("project_id") == project_id and not s.get("is_deleted")
                    and low in s["name"].lower()):
                return sid
        raise ValueError(f"no section {query!r} in {self.project_name(project_id)}")

    def resolve_task(self, query):
        query = str(query).strip()
        if query in self.items:
            return query
        low = query.lower()
        hits = [tid for tid, t in self.items.items()
                if not t.get("is_deleted") and low in t["content"].lower()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ValueError("ambiguous task %r:\n%s" % (
                query, "\n".join(f"{tid}: {self.items[tid]['content']}" for tid in hits)))
        # Server-side fallback: completed tasks drop out of the active-item
        # snapshot, so a write op (reopen/move/delete) must still accept them.
        if _probe_task(query):
            return query
        raise ValueError(f"no task matching {query!r}")

    def resolve_comment(self, query):
        """Return (comment_id, entry) for a comment id or a unique content
        substring. `entry` is {kind: 'task'|'project', owner: id, note: {...}}."""
        query = str(query).strip()
        if query in self.comments_by_id:
            return query, self.comments_by_id[query]
        low = query.lower()
        hits = [cid for cid, e in self.comments_by_id.items()
                if low in (e["note"].get("content") or "").lower()]
        if len(hits) == 1:
            return hits[0], self.comments_by_id[hits[0]]
        if len(hits) > 1:
            raise ValueError("ambiguous comment %r:\n%s" % (
                query, "\n".join(f"{cid}: {self.comments_by_id[cid]['note'].get('content')}"
                                 for cid in hits)))
        raise ValueError(f"no comment matching {query!r}")

    def task_lineage(self, task_id):
        """Ordered chain [{id, content}] from top ancestor -> task."""
        chain = []
        cur = self.items.get(task_id)
        seen = set()
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            chain.insert(0, cur)
            cur = self.items.get(cur.get("parent_id"))
        return chain

    def task_ancestor_ids(self, task_id):
        return [t["id"] for t in self.task_lineage(task_id)[:-1]]

    # ── task filtering ───────────────────────────────────────────────────────
    @staticmethod
    def _prio_from_arg(level):
        return {"p1": 4, "p2": 3, "p3": 2, "p4": 1}.get(level, 1)

    def match(self, item, label=None, status="all", due="all", priority=None):
        if item.get("is_deleted"):
            return False
        if label:
            q = label.strip().lstrip("@").lower()
            if not any(n.lower().lstrip("@") == q for n in self.label_names(item)):
                return False
        if status == "open" and not self.is_open(item):
            return False
        if status == "done" and self.is_open(item):
            return False
        if due and due != "all" and not self._due_match(item, due):
            return False
        if priority:
            want = self._prio_from_arg(priority)
            if item.get("priority", 1) != want:
                return False
        return True

    @staticmethod
    def _due_match(item, spec):
        today = _dt.date.today()
        due = item.get("due") or {}
        date = due.get("date")
        if not date:
            return spec == "no-date"
        if spec == "no-date":
            return False
        try:
            d = _dt.date.fromisoformat(date[:10])
        except ValueError:
            return spec == "any-date"
        if spec == "overdue":
            return d < today
        if spec == "today":
            return d == today
        if spec == "this-week":
            start = today - _dt.timedelta(days=today.weekday())
            end = start + _dt.timedelta(days=6)
            return start <= d <= end
        if spec == "any-date":
            return True
        return True

    # ── hierarchy build (full tree for a project) ────────────────────────────
    def project_tree(self, project_id):
        proj = self.projects.get(project_id, {})
        node = {
            "project": proj,
            "comments": list(self.project_comments.get(project_id, [])),
            "sections": [],
            "tasks": [],
            "subprojects": [],
        }
        top, children = [], {}
        for item in self.items.values():
            if item.get("project_id") != project_id or item.get("is_deleted"):
                continue
            pid = item.get("parent_id")
            if pid and pid in self.items:
                children.setdefault(pid, []).append(item)
            else:
                top.append(item)

        def _sort(lst):
            return sorted(lst, key=lambda i: (i.get("child_order", 0), i.get("day_order", 0)))

        def _task_node(item):
            sn = {"task": item, "subtasks": [],
                  "comments": list(self.comments.get(item["id"], []))}
            sn["subtasks"] = [_task_node(c) for c in _sort(children.get(item["id"], []))]
            return sn

        sectioned = {}
        loose = []
        for item in _sort(top):
            if item.get("section_id"):
                sectioned.setdefault(item["section_id"], []).append(item)
            else:
                loose.append(item)

        sec_ids = {s["id"]: s for s in self.sections.values()
                   if s.get("project_id") == project_id and not s.get("is_deleted")}
        for sid in sorted(sec_ids, key=lambda x: (sec_ids[x].get("section_order", 0), x)):
            node["sections"].append({
                "section": sec_ids[sid],
                "tasks": [_task_node(i) for i in _sort(sectioned.get(sid, []))],
            })
        node["tasks"] = [_task_node(i) for i in _sort(loose)]

        subs = sorted((p for p in self.projects.values()
                       if p.get("parent_id") == project_id and not p.get("is_deleted")),
                      key=lambda p: p.get("child_order", 0))
        node["subprojects"] = [self.project_tree(p["id"]) for p in subs]
        return node

    # ── filtered reduction (keep matches + their ancestor context) ─────────────
    def filter_tree(self, node, **filters):
        """Return a pruned copy of `node` containing only tasks that match the
        filters, plus the ancestor chain (project -> section -> parent tasks)
        required to keep each match in context. A matching task keeps its whole
        subtree. Non-matching projects/sections are dropped entirely."""
        out_tasks = []
        for tn in node["tasks"]:
            sub, _ = self._filter_task(tn, filters)
            if sub is not None:
                out_tasks.append(sub)
        sections = []
        for sec in node["sections"]:
            stasks = []
            for tn in sec["tasks"]:
                sub, _ = self._filter_task(tn, filters)
                if sub is not None:
                    stasks.append(sub)
            if stasks:
                sections.append({"section": sec["section"], "tasks": stasks})
        subprojects = []
        for sub in node["subprojects"]:
            rs = self.filter_tree(sub, **filters)
            if rs["tasks"] or rs["sections"] or rs["subprojects"]:
                subprojects.append(rs)
        return {"project": node["project"], "comments": node["comments"],
                "sections": sections, "tasks": out_tasks,
                "subprojects": subprojects}

    def _filter_task(self, node, filters):
        if self.match(node["task"], **filters):
            return node, True
        kept_sub = []
        for sub in node["subtasks"]:
            rs, k = self._filter_task(sub, filters)
            if rs is not None:
                kept_sub.append(rs)
        if kept_sub:
            return {"task": node["task"], "subtasks": kept_sub,
                    "comments": node.get("comments", [])}, True
        return None, False

    # ── markdown render ──────────────────────────────────────────────────────
    def render_markdown(self, tree):
        lines = []
        self._render_project(tree, lines, depth=0)
        return "\n".join(lines)

    def _render_project(self, node, lines, depth):
        proj = node["project"]
        name = proj.get("name", "?")
        frag = f"`{proj.get('id')}`" if proj.get("id") else ""
        flag = ""
        if proj.get("inbox_project"):
            flag = " [inbox]"
        if proj.get("is_archived"):
            flag += " [archived]"
        indent = "  " * depth
        lines.append(f"{indent}{'#' * min(6, 2 + depth)} Project: {name}  {frag}{flag}")
        if proj.get("description"):
            lines.append(f"{indent}  Description: {proj['description']}")
        for c in node["comments"]:
            lines.append(f"{indent}  Project note: {c.get('content')}")
        for sec in node["sections"]:
            sname = sec["section"].get("name", "?")
            lines.append(f"{indent}  Section: {sname}  `{sec['section'].get('id')}`")
            for tn in sec["tasks"]:
                self._render_task(tn, lines, depth + 2)
        for tn in node["tasks"]:
            self._render_task(tn, lines, depth + 1)
        for sub in node["subprojects"]:
            self._render_project(sub, lines, depth + 1)

    def _render_task(self, node, lines, depth):
        indent = "  " * depth
        t = node["task"]
        when = self.due_obj(t)
        pri = self.priority_name(t)
        labels = self.label_names(t)
        tags = [pri if self.is_open(t) else "DONE"]
        if when:
            tags.append(when)
        if labels:
            tags.append("@" + " @".join(labels))
        box = "[ ]" if self.is_open(t) else "[x]"
        line = f"{indent}- {box} {t['content']}  `{t['id']}`  ({', '.join(tags)})"
        lines.append(line)
        if t.get("description"):
            for dline in t["description"].splitlines():
                lines.append(f"{indent}  {dline}")
        for c in node.get("comments", []):
            lines.append(f"{indent}  > {c.get('content')}")
        for sub in node["subtasks"]:
            self._render_task(sub, lines, depth + 1)


# ── reports (extensible aggregates) ─────────────────────────────────────────
def run_report(snap, name, report_args, as_json):
    import importlib.util
    path = os.path.join(REPORTS_DIR, name if name.endswith(".py") else name + ".py")
    if not os.path.exists(path):
        available = sorted(f[:-3] for f in os.listdir(REPORTS_DIR) if f.endswith(".py"))
        raise ToolError(f"error: no report {name!r}. Available: {available}")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    ctx = {"snapshot": snap, "args": report_args, "json": as_json}
    if hasattr(mod, "run"):
        return mod.run(ctx)
    raise ToolError(f"error: report {name!r} has no run(ctx) function")


# ── CLI command handlers ───────────────────────────────────────────────────
def _all_top_projects(snap):
    return sorted((p for p in snap.projects.values()
                   if not p.get("parent_id") and not p.get("is_deleted")
                   and not p.get("is_archived")),
                  key=lambda p: p.get("child_order", 0))


def cmd_tree(snap, args):
    filters = dict(label=args.label, status=args.status,
                   due=args.due, priority=args.priority)
    if args.project:
        pid = snap.resolve_project(args.project)
        tree = snap.filter_tree(snap.project_tree(pid), **filters)
        tree["root_name"] = snap.project_name(pid)
    else:
        parts = []
        for p in _all_top_projects(snap):
            t = snap.filter_tree(snap.project_tree(p["id"]), **filters)
            if t["tasks"] or t["sections"] or t["subprojects"]:
                parts.append(t)
        tree = {
            "project": {"name": "* ALL *", "id": None, "inbox_project": False,
                        "description": "Top-level projects with at least one match"
                                       " (nested sub-projects included).",
                        "is_archived": False},
            "comments": [], "sections": [], "tasks": [], "subprojects": parts,
        }
    return tree


def cmd_search(snap, args):
    hits = [t for t in snap.items.values()
            if not t.get("is_deleted") and args.text.lower() in t["content"].lower()]
    if args.json:
        print(json.dumps(hits, indent=2, default=str))
    else:
        if not hits:
            print("no matches")
        for t in hits:
            print(f"{t['id']}  {'[ ]' if snap.is_open(t) else '[x]'} {t['content']}  "
                  f"({snap.project_name(t.get('project_id'))}, {snap.priority_name(t)})")
    return 0


def cmd_projects(snap, _a):
    for proj in sorted(snap.projects.values(), key=lambda x: str(x.get("name", ""))):
        n = sum(1 for t in snap.items.values()
                if t.get("project_id") == proj["id"] and snap.is_open(t))
        flag = " [inbox]" if proj.get("inbox_project") else ""
        flag += " [archived]" if proj.get("is_archived") else ""
        print(f"{proj['name']}\t{proj['id']}\topen={n}{flag}")
    return 0


def cmd_labels(snap, _a):
    for lab in sorted(snap.labels.values(), key=lambda x: str(x.get("name", ""))):
        n = sum(1 for t in snap.items.values() if lab["name"] in t.get("labels", []))
        print(f"{lab['name']}\t{lab['id']}\ttasks={n}\tcolor={lab.get('color')}")
    return 0


# ── write commands (v1 REST, same base + token) ─────────────────────────────
def _prio(level):
    return {"p1": 4, "p2": 3, "p3": 2, "p4": 1}.get(level or "p4", 1)


def cmd_add(snap, args):
    body = {"content": args.content}
    project_id = snap.resolve_project(args.project) if args.project else None
    if not project_id:
        project_id = next((p["id"] for p in snap.projects.values()
                           if p.get("inbox_project")), None)
    if project_id:
        body["project_id"] = project_id
        if args.section:
            body["section_id"] = snap.resolve_section(project_id, args.section)
    if args.parent:
        body["parent_id"] = snap.resolve_task(args.parent)
    if args.due:
        body["due_string"] = args.due
    if args.deadline:
        body["deadline_date"] = args.deadline
    body["priority"] = _prio(args.priority)
    if args.labels:
        body["labels"] = [l.strip().lstrip("@") for l in args.labels.split(",") if l.strip()]
    if args.description:
        body["description"] = args.description
    created = _http("POST", "/tasks", body)
    _log_op({"action": "add", "task_id": created.get("id"),
             "project_id": body.get("project_id"), "content": created.get("content"),
             "prev": {}, "new": body})
    print(json.dumps({"created": True, "id": created.get("id"),
                      "content": created.get("content"),
                      "project": snap.project_name(created.get("project_id"))},
                     indent=2))
    return 0


def _resolve_or_exit(snap, query):
    try:
        return snap.resolve_task(query)
    except ValueError as e:
        raise ToolError(f"error: {e}")


def _task_label(snap, tid):
    """Task content for confirmation messages. Falls back gracefully because a
    write op (e.g. reopen) may target a task that just left the snapshot."""
    t = snap.items.get(tid)
    return t["content"] if t else f"<task {tid}> (completed; not in current snapshot)"


def cmd_complete(snap, args):
    tid = _resolve_or_exit(snap, args.task)
    prev = _task_prev(snap, tid)
    _http("POST", f"/tasks/{tid}/close", {})
    _log_op({"action": "complete", "task_id": tid, "content": prev.get("content"),
             "prev": prev, "new": {"checked": True}})
    print(f"completed {tid}: {_task_label(snap, tid)}")
    return 0


def cmd_reopen(snap, args):
    tid = _resolve_or_exit(snap, args.task)
    prev = _task_prev(snap, tid)
    _http("POST", f"/tasks/{tid}/reopen", {})
    _log_op({"action": "reopen", "task_id": tid, "content": prev.get("content"),
             "prev": prev, "new": {"checked": False}})
    print(f"reopened {tid}: {_task_label(snap, tid)}")
    return 0


def cmd_delete(snap, args):
    tid = _resolve_or_exit(snap, args.task)
    prev = _task_prev(snap, tid)
    _http("DELETE", f"/tasks/{tid}")
    _log_op({"action": "delete", "task_id": tid, "content": prev.get("content"),
             "prev": prev, "new": {"deleted": True}})
    print(f"deleted {tid}: {_task_label(snap, tid)}")
    return 0


def cmd_move(snap, args):
    tid = _resolve_or_exit(snap, args.task)
    t = snap.items.get(tid, {})
    prev = {"project_id": t.get("project_id"), "section_id": t.get("section_id"),
            "parent_id": t.get("parent_id")}
    pid = snap.resolve_project(args.project)
    body = {"project_id": pid}
    if args.section:
        body["section_id"] = snap.resolve_section(pid, args.section)
    if args.parent:
        body["parent_id"] = snap.resolve_task(args.parent)
    _http("POST", f"/tasks/{tid}/move", body)
    _log_op({"action": "move", "task_id": tid, "content": t.get("content"),
             "project_id": pid,
             "from": prev,
             "to": {"project_id": pid, "section_id": body.get("section_id"),
                    "parent_id": body.get("parent_id")},
             "prev": prev, "new": body})
    loc = snap.project_name(pid)
    if body.get("section_id"):
        loc += " / " + snap.section_name(body["section_id"])
    if body.get("parent_id"):
        loc += " / parent " + body["parent_id"]
    print(f"moved {tid} -> {loc}")
    return 0


def cmd_update(snap, args):
    tid = _resolve_or_exit(snap, args.task)
    prev = _task_prev(snap, tid)
    body = {}
    if args.content:
        body["content"] = args.content
    if args.priority:
        body["priority"] = _prio(args.priority)
    if args.due:
        body["due_string"] = args.due
    if args.deadline is not None:
        body["deadline_date"] = args.deadline or None
    if args.labels:
        body["labels"] = [l.strip().lstrip("@") for l in args.labels.split(",") if l.strip()]
    if args.description is not None:
        body["description"] = args.description
    _http("POST", f"/tasks/{tid}", body)
    _log_op({"action": "update", "task_id": tid, "content": prev.get("content"),
             "project_id": prev.get("project_id"), "prev": prev, "new": body})
    print(f"updated {tid}: {_task_label(snap, tid)}")
    return 0


def cmd_comment(snap, args):
    tid = _resolve_or_exit(snap, args.task)
    created = _http("POST", "/comments", {"task_id": tid, "content": args.content})
    _log_op({"action": "comment", "task_id": tid, "comment_id": created.get("id"),
             "content": args.content, "prev": {}, "new": {"content": args.content}})
    print(f"commented on {tid}: {args.content}")
    return 0


def cmd_delete_comment(snap, args):
    cid, entry = snap.resolve_comment(args.comment)
    note = entry["note"]
    prev = {"kind": entry["kind"], "owner": entry["owner"],
            "content": note.get("content"), "posted_at": note.get("posted_at")}
    _http("DELETE", f"/comments/{cid}")
    _log_op({"action": "delete-comment", "comment_id": cid,
             "task_id": entry["owner"] if entry["kind"] == "task" else None,
             "project_id": entry["owner"] if entry["kind"] == "project" else None,
             "prev": prev, "new": {"deleted": True}})
    where = "task" if entry["kind"] == "task" else "project"
    print(f"deleted comment on {where} {entry['owner']}: {note.get('content')}")
    return 0


# ── undo (reverse an op from the log) ───────────────────────────────────────
def _undo_add(snap, rec):
    _http("POST", f"/tasks/{rec['task_id']}/close", {})   # add -> complete


def _undo_complete(snap, rec):
    _http("POST", f"/tasks/{rec['task_id']}/reopen", {})


def _undo_reopen(snap, rec):
    _http("POST", f"/tasks/{rec['task_id']}/close", {})


def _undo_delete(snap, rec):
    prev = rec.get("prev", {})
    raise ToolError("error: delete is not reversible server-side. To restore, run add "
                    f"again: content={prev.get('content')!r} labels={prev.get('labels')!r}")


def _undo_move(snap, rec):
    tid = rec["task_id"]; prev = rec.get("prev", {})
    body = {}
    if prev.get("project_id"):
        body["project_id"] = prev["project_id"]
    if prev.get("section_id"):
        body["section_id"] = prev["section_id"]
    if prev.get("parent_id"):
        body["parent_id"] = prev["parent_id"]
    _http("POST", f"/tasks/{tid}/move", body)


def _undo_update(snap, rec):
    tid = rec["task_id"]; prev = rec.get("prev", {})
    body = {}
    if prev.get("content") is not None:
        body["content"] = prev["content"]
    if prev.get("priority"):
        body["priority"] = prev["priority"]
    if prev.get("labels") is not None:
        body["labels"] = prev["labels"]
    if prev.get("description") is not None:
        body["description"] = prev["description"]
    if prev.get("due"):                      # v1 rejects a null due; leave as-is
        body["due_string"] = prev["due"].get("string") or prev["due"].get("date")
    dl = prev.get("deadline")
    if dl is not None:
        body["deadline_date"] = dl.get("date") if isinstance(dl, dict) else dl
    if not body:
        return
    _http("POST", f"/tasks/{tid}", body)


def _undo_comment(snap, rec):
    """Delete the comment an `add comment` op created. Falls back to a
    same-task/same-content match because a prior delete-comment + its undo may
    have replaced the comment (new id), leaving the logged id stale."""
    cid, tid, content = rec.get("comment_id"), rec.get("task_id"), rec.get("content")
    if cid and cid in snap.comments_by_id:
        _http("DELETE", f"/comments/{cid}")
        return
    if tid and content:
        for c2, e in snap.comments_by_id.items():
            if (e["kind"] == "task" and e["owner"] == tid
                    and (e["note"].get("content") or "").strip() == str(content).strip()):
                _http("DELETE", f"/comments/{c2}")
                return
    raise ToolError("error: comment not found (on id or same-content); cannot undo")


def _undo_delete_comment(snap, rec):
    prev = rec.get("prev", {})
    body = {"content": prev.get("content", "")}
    if prev.get("kind") == "project":
        body["project_id"] = prev.get("owner") or rec.get("project_id")
    else:
        body["task_id"] = rec.get("task_id") or prev.get("owner")
    if body.get("content"):
        _http("POST", "/comments", body)
    else:
        raise ToolError("error: comment content not logged; cannot recreate")


UNDO_ACTIONS = {
    "add": _undo_add,
    "complete": _undo_complete,
    "reopen": _undo_reopen,
    "delete": _undo_delete,
    "move": _undo_move,
    "update": _undo_update,
    "comment": _undo_comment,
    "delete-comment": _undo_delete_comment,
}


def cmd_undo(snap, args):
    ops = _read_ops()
    rec = next((o for o in ops
                if o.get("op_id") == args.op or str(o.get("op_id")).endswith(args.op)),
               None)
    if rec is None:
        raise ToolError(f"error: no op {args.op!r} in {OP_LOG}. Run `ops` to list.")
    handler = UNDO_ACTIONS.get(rec.get("action"))
    if handler is None:
        raise ToolError(f"error: op {rec.get('op_id')} ({rec.get('action')}) cannot be undone")
    handler(snap, rec)
    _log_op({"action": "undo", "undo_of": rec["op_id"], "of_action": rec.get("action"),
             "task_id": rec.get("task_id"), "comment_id": rec.get("comment_id")})
    print(f"undone op {rec['op_id']} ({rec.get('action')})")
    return 0


def cmd_ops(snap, args):
    ops = _read_ops()
    if args.last:
        ops = ops[-args.last:]
    for o in ops:
        a = o.get("action")
        ts = (o.get("ts") or "")[:19]
        if a == "undo":
            print(f"{o.get('op_id')}  {ts}  undo of {o.get('undo_of')}")
            continue
        if o.get("task_id"):
            label = (o.get("content")
                     or (o.get("prev", {}) or {}).get("content")
                     or o.get("task_id"))
            print(f"{o.get('op_id')}  {ts}  {a:<14} task={o.get('task_id')}  {label}")
        else:
            print(f"{o.get('op_id')}  {ts}  {a:<14} comment={o.get('comment_id')}")
    return 0


def cmd_report(snap, args):
    kwargs = {}
    for kv in args.arg:
        if "=" in kv:
            k, v = kv.split("=", 1)
            kwargs[k] = v
    out = run_report(snap, args.report, kwargs, args.json)
    if out is not None:
        if args.json and not isinstance(out, str):
            print(json.dumps(out, indent=2, default=str))
        else:
            print(out)
    return 0


# ── dispatcher: parse kwargs -> Namespace, run like the original main() ─────
def _args(command, kw):
    g = kw.get
    if command == "tree":
        return _NS(label=g("label", ""), status=g("status", "open"),
                   due=g("due", "all"), priority=g("priority", ""),
                   project=g("project", ""), format=g("format", "tree"))
    if command == "search":
        return _NS(text=g("text", ""), json=bool(g("json", False)))
    if command in ("projects", "labels"):
        return _NS()
    if command == "add":
        return _NS(content=g("content", ""), project=g("project", ""),
                   section=g("section", ""), parent=g("parent", ""),
                   due=g("due", ""), deadline=g("deadline", ""),
                   priority=g("priority", "p4"), labels=g("labels", ""),
                   description=g("description", ""))
    if command in ("complete", "reopen", "delete"):
        return _NS(task=g("task", ""))
    if command == "move":
        return _NS(task=g("task", ""), project=g("project", ""),
                   section=g("section", ""), parent=g("parent", ""))
    if command == "update":
        return _NS(task=g("task", ""), content=g("content", ""),
                   priority=g("priority", ""), due=g("due", ""),
                   deadline=g("deadline", None), labels=g("labels", ""),
                   description=g("description", None))
    if command == "comment":
        return _NS(task=g("task", ""), content=g("content", ""))
    if command == "delete-comment":
        return _NS(comment=g("comment", ""))
    if command == "undo":
        return _NS(op=g("op", ""))
    if command == "ops":
        return _NS(last=g("last", 20))
    if command == "report":
        return _NS(report=g("report", ""), arg=list(g("arg", []) or []),
                   json=bool(g("json", False)))
    raise ToolError(f"unknown command {command!r}")


_COMMAND_FUNCS = {
    "tree": cmd_tree, "projects": cmd_projects, "labels": cmd_labels,
    "search": cmd_search, "add": cmd_add, "complete": cmd_complete,
    "reopen": cmd_reopen, "delete": cmd_delete, "move": cmd_move,
    "update": cmd_update, "comment": cmd_comment, "delete-comment": cmd_delete_comment,
    "undo": cmd_undo, "ops": cmd_ops, "report": cmd_report,
}

FORMAT_ALIASES = {}  # the `compact` alias was deliberately rejected; see cli.


def run_command(command, **kwargs):
    """Execute ``command`` with the given keyword args. Returns a dict:

      {ok: True,  cmd, output: str}
      {ok: False, cmd, error: str}
      {ok: False, cmd, rate_limited: True, retry_after, strikes, hard_stop}

    Captures stdout from the handler (so the MCP tool returns strings and the
    CLI prints them) and applies the original ``main()`` tree formatting.
    """
    result = {"ok": False, "cmd": command, "error": None,
              "rate_limited": False, "retry_after": 0.0, "strikes": 0,
              "hard_stop": False, "output": ""}
    try:
        no_net = os.environ.get("TODOIST_LAYER_NO_NET") == "1"
        args = _args(command, kwargs)
        snap = Todoist(fresh=not no_net)

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = _COMMAND_FUNCS[command](snap, args)

        output = buf.getvalue()
        # tree handling matches the original main(): func returns the tree dict.
        if command == "tree" and code is not None:
            tree = code
            fmt = args.format
            rendered = []
            if fmt in ("json", "both"):
                rendered.append(json.dumps(tree, indent=2, default=str))
            if fmt in ("tree", "md", "both"):
                rendered.append(snap.render_markdown(tree))
            if rendered:
                output = "\n".join(rendered) + "\n"

        result["ok"] = True
        result["output"] = output
    except RateLimited as e:
        result["rate_limited"] = True
        result["retry_after"] = e.retry_after
        result["strikes"] = e.strikes
        result["hard_stop"] = e.hard_stop
        result["error"] = str(e)
    except (ToolError, ValueError) as e:
        result["error"] = str(e)
    except Exception as e:  # never let the MCP server die on unexpected errors
        result["error"] = f"error: {type(e).__name__}: {e}"
    return result