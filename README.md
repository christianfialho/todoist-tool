# todoist-tool

A Todoist integration for people who use Todoist through both a terminal and
an MCP-capable AI agent. One core library (`todoist/core.py`) drives two
front-ends — a thin CLI and a stdio MCP server — with a single shared
dispatcher (`run_command`), a shared JSONL operation log, and full undo.

## Why

The Todoist web/API surface is flat: everything is project-scoped, there is no
hierarchy view, and destructive operations are one-way. This tool adds the
layer that's missing:

- **Whole-hierarchy views** — `tree` renders project → section → parent →
  subtask in one call, instead of N API round-trips.
- **An audit + undo log** — every mutating operation is appended to a JSONL
  op log; `undo` reverses the last operation (and `ops` shows the audit
  trail). Completes are reopened, renames are reversed, buckets are restored.
- **Buckets (project lifecycle)** — a "bucket" is an outcome you work inside
  (a paper, a client, a trip). Bucket commands map onto Todoist project
  objects via rename-only lifecycle management with a fixed reserve of spare
  slots, so the account's project object count never changes; the slot
  mechanics stay invisible to the caller.
- **A rate-limit gate with memory** — an in-process gate tracks 429 strikes,
  backs off, and survives restarts via a small on-disk state file, plus a
  short-TTL sync snapshot cache so repeated agent calls don't re-fetch the
  whole account.

## Layout

```
todoist/core.py     the entire integration: HTTP/sync layer, State gate,
                    cmd_* handlers, op log, report plugins, argparse surface
todoist/cli.py      thin CLI wrapper over run_command (help strings included)
todoist_mcp/server.py   stdio MCP server exposing two tools: `todoist`
                    (everyday task ops) and `buckets` (project lifecycle)
tests/              safety tests for the bucket invariant + CLI mapping
```

## Setup

Python 3.10+. No third-party dependencies for the CLI (stdlib only); the MCP
server needs the `mcp` package.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install mcp          # only needed for the MCP server
```

Store your API token in a dotenv-style file. Default location:

```
~/.config/todoist-tool/.env
```

with either `TODOIST_API_KEY=<token>` or `TODOIST_API_TOKEN=<token>`.
Override the location with `TODOIST_ENV_FILE=/path/to/.env`.

### CLI

```bash
python3 todoist/cli.py tree                 # full hierarchy, one call
python3 todoist/cli.py add -p Home -t "..." 
python3 todoist/cli.py complete <id>
python3 todoist/cli.py undo                 # reverse the last operation
python3 todoist/cli.py ops                  # audit trail
python3 todoist/cli.py create "Q4 paper"    # open a new bucket
python3 todoist/cli.py release <bucket>     # close out + free the slot
python3 todoist/cli.py report <name>        # aggregation report plugin
```

`python3 todoist/cli.py --help` documents every command and flag.

### MCP server

Register in any MCP client's config (stdio transport):

```json
{
  "mcpServers": {
    "todoist": {
      "command": "/path/to/todoist-tool/.venv/bin/python",
      "args": ["/path/to/todoist-tool/todoist_mcp/server.py"],
      "env": { "TODOIST_ENV_FILE": "/path/to/.env" }
    }
  }
}
```

Two tools are exposed on one server: `todoist` (view/add/complete/move/
update/comment/search/report/undo) and `buckets` (create/rename/release/
status). Both return plain JSON in a single text payload.

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `TODOIST_ENV_FILE` | `~/.config/todoist-tool/.env` | token file |
| `TODOIST_STATE_HOME` | `~/.local/state/todoist-tool` | op log + reports root |
| `TODOIST_RATELIMIT_STATE` | `<package>/.ratelimit.json` | 429-gate state file |
| `TODOIST_SYNC_TTL` | `5.0` seconds | sync snapshot cache TTL |
| `TODOIST_OP_LOG` / `TODOIST_REPORTS_DIR` | under state home | finer overrides |

## Bucket invariant

The design invariant (and what the tests enforce): bucket commands **never**
create, delete, or archive a project object. They only rename between a
`Spare Project N` marker and a live outcome name, so the object count is
immutable and every transition is a rename — which `undo` can always reverse.

## Tests

```bash
python3 -m pytest tests/ -q
```

16 tests cover the bucket slot invariant (any emit of create/delete/archive
fails the test) and the CLI → core command mapping.
