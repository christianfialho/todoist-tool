#!/usr/bin/env python3
"""Todoist thin CLI — a wrapper over todoist.core.run_command for manual use.

All logic lives in core; this file only defines the argparse surface (so
``--help`` stays accurate and muscle-memory flags keep working). It is a
faithful port of the original ``todoist_layer.py`` CLI, with added ``help=``
strings on ``--format``/``--status``/``--due``/``--priority`` so ``-h`` can
explain the accepted values.

Runs identically via ``python3 todoist/cli.py ...`` or through the historical
shim at ``~/.hermes/profiles/christian/scripts/todoist_layer.py``.
"""

import argparse
import os
import sys

# Make `todoist` importable when run directly as `python3 todoist/cli.py`
# (sys.path[0] is the script's own dir, not the project root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from todoist import core


def _build_parser():
    ap = argparse.ArgumentParser(
        prog="todoist_layer",
        description="Todoist abstraction layer — full hierarchy in one call.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("tree", help="Print the full project -> section -> parent -> subtask tree.")
    p.add_argument("--project", help="Project name or id (default: all active projects).")
    p.add_argument("--label", help="Only tasks carrying this label (e.g. @waiting).")
    p.add_argument("--status", choices=["open", "all", "done"], default="open",
                   help="open=not checked, all=everything, done=checked. Default: open")
    p.add_argument("--due", choices=["all", "today", "this-week", "overdue", "no-date", "any-date"],
                   default="all",
                   help="all=no filter, today, this-week, overdue, no-date, any-date. Default: all")
    p.add_argument("--priority", choices=["p1", "p2", "p3", "p4"] or None, default=None,
                   help="Only tasks at user-facing priority P1..P4 (p1 highest).")
    p.add_argument("--format", choices=["tree", "md", "json", "both"], default="tree",
                   help="tree=ASCII hierarchy, md=markdown for chat, json=machine-readable, "
                        "both=json then md. Default: tree")
    p.set_defaults(func=core.cmd_tree)

    sub.add_parser("projects", help="List projects with id + open task count.").set_defaults(func=core.cmd_projects)
    sub.add_parser("labels", help="List labels with id + color.").set_defaults(func=core.cmd_labels)

    s = sub.add_parser("search", help="Search open/closed task text.")
    s.add_argument("text")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=core.cmd_search)

    w = sub.add_parser("add", help="Create a task.")
    w.add_argument("--content", required=True)
    w.add_argument("--project", help="Project name/id (default: Inbox).")
    w.add_argument("--section", help="Section name within the project.")
    w.add_argument("--parent", help="Parent task id or substring to nest under.")
    w.add_argument("--due", help="Due string, e.g. 'next Monday' or '2026-10-01'.")
    w.add_argument("--deadline", help="Hard deadline YYYY-MM-DD.")
    w.add_argument("--priority", choices=["p1", "p2", "p3", "p4"], default="p4")
    w.add_argument("--labels", help="Comma-separated label names (no @ needed).")
    w.add_argument("--description", help="Task description/notes.")
    w.set_defaults(func=core.cmd_add)

    c = sub.add_parser("complete", help="Mark a task complete.")
    c.add_argument("--task", required=True, help="Task id or unique content substring.")
    c.set_defaults(func=core.cmd_complete)

    r = sub.add_parser("reopen", help="Reopen a completed task.")
    r.add_argument("--task", required=True)
    r.set_defaults(func=core.cmd_reopen)

    d = sub.add_parser("delete", help="Delete a task permanently.")
    d.add_argument("--task", required=True)
    d.set_defaults(func=core.cmd_delete)

    m = sub.add_parser("move", help="Move a task to a project/section/parent.")
    m.add_argument("--task", required=True)
    m.add_argument("--project", required=True)
    m.add_argument("--section")
    m.add_argument("--parent")
    m.set_defaults(func=core.cmd_move)

    u = sub.add_parser("update", help="Update task fields (priority/description/labels/due).")
    u.add_argument("--task", required=True)
    u.add_argument("--content")
    u.add_argument("--priority", choices=["p1", "p2", "p3", "p4"])
    u.add_argument("--due")
    u.add_argument("--deadline")
    u.add_argument("--description")
    u.add_argument("--labels")
    u.set_defaults(func=core.cmd_update)

    cc = sub.add_parser("comment", help="Add a comment to a task.")
    cc.add_argument("--task", required=True)
    cc.add_argument("--content", required=True)
    cc.set_defaults(func=core.cmd_comment)

    dc = sub.add_parser("delete-comment", help="Delete a comment by id or unique text.")
    dc.add_argument("--comment", required=True, help="Comment id or unique content substring.")
    dc.set_defaults(func=core.cmd_delete_comment)

    uu = sub.add_parser("undo", help="Reverse an operation from the op log.")
    uu.add_argument("op", help="Op id (e.g. op-002) from `ops`.")
    uu.set_defaults(func=core.cmd_undo)

    oo = sub.add_parser("ops", help="List recent recorded operations (audit + undo).")
    oo.add_argument("--last", type=int, default=20, help="Show only the last N ops.")
    oo.set_defaults(func=core.cmd_ops)

    rep = sub.add_parser("report", help="Run an aggregation report plugin.")
    rep.add_argument("--report", required=True, help="Report name in todoist_reports/")
    rep.add_argument("--arg", action="append", default=[], help="key=value report args (repeatable).")
    rep.add_argument("--json", action="store_true")
    rep.set_defaults(func=core.cmd_report)
    return ap


def main(argv=None):
    ns = _build_parser().parse_args(argv)
    kw = {k: v for k, v in vars(ns).items() if k not in ("cmd", "func")}
    res = core.run_command(ns.cmd, **kw)
    if res["ok"]:
        sys.stdout.write(res["output"])
        if res["output"] and not res["output"].endswith("\n"):
            sys.stdout.write("\n")
        return 0
    sys.stderr.write(res["error"] + "\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())