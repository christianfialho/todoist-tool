#!/usr/bin/env python3
"""Todoist MCP stdio server — two tools.

Splits the surface into two ergonomic handles on the SAME stdio server, both
delegating to the single ``todoist.core.run_command`` dispatcher with one
shared op log:

  * ``todoist`` — everyday task commands (view / add / complete / move /
    update / comment / search / report / audit+undo for task ops).
  * ``buckets`` — the project/outcome lifecycle. This is where the project-
    slot mechanics are hidden: the user just says "create/rename/release a
    bucket" and never sees that a bucket maps to a Todoist project object and
    that reusing a slot is a *rename*, not a create/delete. ``undo`` here
    reverses a create/rename/release without the user knowing about the task
    machinery underneath.

The ``project``-slot mechanics live entirely in core (see core.py:66-70):
bucket commands NEVER create/delete/archive a Project object — they only
rename between a "Spare Project N" marker and a live outcome name, so the
Todoist object count (the grandfathered limit) never changes.

Both tools are ``structured_output=False`` (plain JSON in a single text
payload — avoids the SDK's scalar-return payload duplication).
"""

import json as _json
import os
import sys
from typing import Literal, Optional

# Make `todoist` importable when run directly from any checkout location.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from todoist.core import run_command  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402


class Server(MCPServer):
    """`todoist` (tasks) + `buckets` (project/outcome lifecycle)."""

    def __init__(self):
        super().__init__(
            "todoist",
            version="1.1.0",
            description=(
                "Todoist abstraction layer. Two tools on one server: "
                "'todoist' for everyday task work (tree/search/add/complete/"
                "move/update/comment/report/ops/undo) and 'buckets' for the "
                "outcome/project lifecycle (create/rename/release/status/list/"
                "undo). Slot management is hidden inside 'buckets'."
            ),
        )

        @self.tool(name="todoist", structured_output=False, description=(
            "Everyday Todoist task commands. `command` selects the operation; "
            "`bucket` scopes a command to one project (the outcome/area the "
            "task lives under). Returns JSON: {ok:true,cmd,output} on success, "
            "{ok:false,cmd,error} on failure, or {ok:false,rate_limited:true,"
            "retry_after,strikes} when rate-limited. Use command='tree' for the "
            "whole hierarchy, 'search' for text, 'add'/'complete'/'reopen'/"
            "'move'/'update'/'delete'/'comment' for task writes, and "
            "'ops'/'undo' for the shared audit log."))
        def todoist(
            command: Literal["tree", "labels", "search",
                             "add", "complete", "reopen", "move", "update",
                             "comment", "delete-comment", "delete", "undo",
                             "ops", "report"],
            bucket: Optional[str] = None,
            label: Optional[str] = None,
            status: Optional[Literal["open", "all", "done"]] = None,
            due: Optional[Literal["all", "today", "this-week", "overdue",
                                  "no-date", "any-date"]] = None,
            priority: Optional[Literal["p1", "p2", "p3", "p4"]] = None,
            format: Optional[Literal["tree", "md", "json"]] = None,
            text: Optional[str] = None,
            task: Optional[str] = None,
            content: Optional[str] = None,
            section: Optional[str] = None,
            parent: Optional[str] = None,
            deadline: Optional[str] = None,
            labels: Optional[str] = None,
            description: Optional[str] = None,
            comment: Optional[str] = None,
            op: Optional[str] = None,
            last: Optional[int] = 20,
            report: Optional[str] = None,
            arg: Optional[list] = [],
            json: Optional[bool] = False,
        ) -> str:
            kw = dict(
                bucket=bucket, label=label, status=status, due=due,
                priority=priority, format=format, text=text, task=task,
                content=content, section=section, parent=parent,
                deadline=deadline, labels=labels, description=description,
                comment=comment, op=op, last=last, report=report, arg=arg,
                json=json,
            )
            kw = {k: v for k, v in kw.items() if v is not None}
            res = run_command(command, **kw)
            return _json.dumps(res)

        @self.tool(name="buckets", structured_output=False, description=(
            "Manage outcomes / projects ('buckets') without touching Todoist "
            "project-slot limits. `command` in {create, rename, release, "
            "status, list, undo, ops}. "
            "create(name): open a new outcome, transparently reusing a free "
            "reserve slot. rename(bucket,name): retitle one. "
            "release(name): close out an outcome's open tasks and return it "
            "to the free-slot pool so its slot can be reused by a future "
            "create. status: live vs free-slot counts. list: all buckets with "
            "open-task counts. Undo here reverses a create/rename/release and "
            "reopens any tasks release closed. Returns JSON of the same shape "
            "as the todoist tool."))
        def buckets(
            command: Literal["create", "rename", "release", "status", "list",
                             "undo", "ops"],
            bucket: Optional[str] = None,
            name: Optional[str] = None,
            op: Optional[str] = None,
            last: Optional[int] = 20,
        ) -> str:
            kw = dict(bucket=bucket, name=name, op=op, last=last)
            kw = {k: v for k, v in kw.items() if v is not None}
            res = run_command(command, **kw)
            return _json.dumps(res)


server = Server()


def main():
    server.run(transport="stdio")


if __name__ == "__main__":
    main()