#!/usr/bin/env python3
"""Non-interactive $EDITOR for `hermes config edit`.

Replaces the dead hosted Todoist MCP block with the local stdio server entry.
`hermes config set` cannot DELETE keys (url/auth/enabled must go, otherwise
mcp's ``_is_http()`` sees ``url`` present and forces HTTP transport), so the
removal is routed through the standard ``hermes config edit`` command with this
script as $EDITOR. It rewrites the exact block in-place (no YAML reorder).
"""
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    text = f.read()

old = (
    "  todoist:\n"
    "    url: https://ai.todoist.net/mcp\n"
    "    auth: oauth\n"
    "    enabled: false"
)
new = (
    "  todoist:\n"
    "    command: /home/fialho/todoist-tool/todoist_mcp/server.py"
)

if old not in text:
    raise SystemExit("ERROR: could not find the dead hosted todoist block in " + path)

text = text.replace(old, new)
with open(path, "w", encoding="utf-8") as f:
    f.write(text)
print("todoist MCP block rewritten (url/auth/enabled removed, command set)")