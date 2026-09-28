#!/home/fialho/todoist-tool/.venv/bin/python
"""Todoist MCP stdio server.

Exposes ONE tool, ``todoist``, with a ``command: Literal[...]`` enum — the
single-handle pattern proven by the scratch server. All logic lives in
``todoist.core.run_command``; this module is only the transport.

Why a long-lived server (vs the CLI)? Real per-session in-memory state: the
rate-limit gate and short-TTL sync cache persist across tool calls without a
shell round-trip, and the schema enumerates valid values so wrong guesses are
structurally impossible.
"""

import json as _json
import sys
from typing import Literal, Optional

sys.path.insert(0, "/home/fialho/todoist-tool")

from todoist.core import run_command  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402


class Server(MCPServer):
    """`todoist` single tool."""

    def __init__(self):
        super().__init__(
            "todoist",
            version="1.0.0",
            description=(
                "Todoist abstraction layer. Manage tasks, projects, sections, "
                "labels and comments from a full-hierarchy snapshot. Use "
                "command='tree' to see the whole tree, 'search' for task text, "
                "'add'/'complete'/'reopen'/'move'/'update'/'delete' for write "
                "ops, 'ops'/'undo' for the audit log, 'report' for aggregates."
            ),
        )

        @self.tool(name="todoist", structured_output=False, description=(
            "Run a Todoist command. `command` selects the operation; the other "
            "options are the command's arguments. Returns JSON of the form "
            "{ok:true, cmd, output} on success, {ok:false, cmd, error} on "
            "failure, or {ok:false, rate_limited:true, retry_after, strikes} "
            "when rate-limited (retry after retry_after seconds)."))
        def todoist(
            # --- read commands ---
            command: Literal["tree", "projects", "labels", "search",
                             "add", "complete", "reopen", "move", "update",
                             "comment", "delete-comment", "delete", "undo",
                             "ops", "report"],
            project: Optional[str] = None,
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
                project=project, label=label, status=status, due=due,
                priority=priority, format=format, text=text, task=task,
                content=content, section=section, parent=parent,
                deadline=deadline, labels=labels, description=description,
                comment=comment, op=op, last=last, report=report, arg=arg,
                json=json,
            )
            kw = {k: v for k, v in kw.items() if v is not None}
            res = run_command(command, **kw)
            return _json.dumps(res)


server = Server()


def main():
    server.run(transport="stdio")


if __name__ == "__main__":
    main()