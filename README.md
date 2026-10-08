# todoist-tool

I use Todoist from two places, a terminal and an MCP-capable AI agent, and I
wanted both to hit the same core: one library (`todoist/core.py`) with a thin
CLI on one side and a stdio MCP server on the other, a shared JSONL operation
log, and undo everywhere.

## Why

The Todoist web/API surface is flat. Everything is project-scoped, there's no
hierarchy view, and destructive operations are one-way. So this tool adds:

- `tree`, which renders project → section → parent → subtask in one call
  instead of N API round-trips.
- An op log and undo. Every mutating operation is appended to a JSONL file;
  `undo` reverses the last one (completes get reopened, renames reversed,
  buckets restored) and `ops` shows the audit trail.
- Buckets. A bucket is an outcome you work inside (a paper, a client, a trip),
  and bucket commands map onto Todoist project objects through rename-only
  lifecycle management with a fixed reserve of spare slots. The account's
  project count never changes and the slot bookkeeping stays invisible; see
  the invariant section below, and the tests that enforce it.
- A rate-limit gate with memory. An in-process gate tracks 429s and backs
  off, and a small on-disk state file keeps that memory across restarts.
  There's also a short-TTL sync snapshot cache so an agent asking three
  questions in a row doesn't re-fetch the whole account each time.

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

The rule the tests enforce: bucket commands never create, delete, or archive a
project object. They only rename between a `Spare Project N` marker and a live
outcome name, so the object count is immutable and every transition is a
rename, which means `undo` can always reverse it.

## Tests

```bash
python3 -m pytest tests/ -q
```

16 tests cover the bucket slot invariant (any emit of create/delete/archive
fails the test) and the CLI → core command mapping.
