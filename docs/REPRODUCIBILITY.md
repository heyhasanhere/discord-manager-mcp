# Discord Message Manager — Reproducibility Guide

This document describes how to set up the project, run it, and verify that everything works. A person following these steps should be able to reproduce a working environment without guessing what was done before.

## Prerequisites

- Python 3.10 or later (the code uses `from __future__ import annotations` and union types like `str | None`)
- A Discord bot token with permissions to read messages, search members, and delete messages in the target server
- The target Discord guild ID (server ID)

## Step 1 — Get a copy of the project

```bash
git clone <repo-url> discord-manager-mcp
cd discord-manager-mcp
```

If you already have a copy, make sure it is up to date:

```bash
git checkout main
git pull
```

The repo has two commits on `main`:

| Commit hash | Message |
|-------------|---------|
| `7ddc24a` | Add Discord message management MCP server |
| `3974cdf` | Harden Discord message deletion |

Run `git rev-parse HEAD` to confirm you are at the latest commit.

## Step 2 — Set up a Python environment

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the project dependencies (if a `requirements.txt` or `pyproject.toml` exists, use it; otherwise install manually):

```bash
pip install mcp httpx pytest
```

The code imports these libraries directly:

- `mcp.server.MCPServer` — MCP server framework
- `httpx.AsyncClient`, `httpx.Response` — async HTTP client (for Discord API calls and mock transports in tests)
- Standard library modules (`asyncio`, `dataclasses`, `datetime`, `secrets`, `time`, `collections`)

## Step 3 — Set environment variables

The server requires two environment variables. It will refuse to start without them:

| Variable | Purpose | Example value |
|----------|---------|---------------|
| `DISCORD_BOT_TOKEN` | Bot authentication token from the Discord Developer Portal | `MTIzNDU2Nzg5MDEyMzQ1Njc4.AbCdEf.ghIjKlMnOpQrStUvWxYz012345` |
| `DISCORD_GUILD_ID` | Numeric ID of the Discord server the bot manages | `987654321098765432` |

Set them in your shell before starting:

```bash
export DISCORD_BOT_TOKEN="your-bot-token-here"
export DISCORD_GUILD_ID="your-guild-id-here"
```

**Important:** Do not commit these values. The `.gitignore` file excludes `__pycache__/`, `*.pyc`, `.DS_Store`, and `graphify-out/`. It does not explicitly list env files, so do not add secrets to the repository.

## Step 4 — Start the server (standalone)

The MCP server can be run directly:

```bash
python server.py
```

This calls `main()` in `server.py`, which:

1. Reads `DISCORD_BOT_TOKEN` and `DISCORD_GUILD_ID` from environment variables.
2. Exits with a clear error if either is missing.
3. Creates a `DiscordRestApi` instance bound to the specified guild.
4. Calls `create_server(api)` to register the four MCP tools on an `MCPServer`.
5. Starts the server with stdio transport (`server.run(transport="stdio")`).

The server stays running and communicates with Hermes over standard input/output (JSON-RPC messages).

## Step 5 — Configure Hermes

The MCP entry is installed as `discord-message-manager` in the current Hermes profile. To verify:

1. Open your Hermes configuration for the active profile.
2. Look under the MCP servers section for an entry named `discord-message-manager`.
3. Ensure it points to the project's `server.py` file and sets up stdio transport.

After changing the profile configuration, start a **new** Hermes session (do not just reload) so the updated MCP server is loaded.

## Step 6 — Run tests

Tests verify both business logic and safety behavior:

```bash
python -m unittest discover -s tests -v
```

The full test path used in this project:

```bash
/Users/falcon/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -v
```

### What the test suite checks

**test_discord_manager.py (main test file)**

| Test class | Tests |
|-----------|-------|
| `BuildSummaryTests` | Summary correctly counts messages by channel and author, includes oldest/newest timestamps |
| `FilterMessagesTests` | Filtering works for: people IDs, channel IDs, exclusion lists, exact message IDs, time ranges (`after`/`before`), text content search (case-insensitive) |
| `DeleteServiceTests` | Preview rejects empty requests; preview never deletes; confirm deletes only the previewed messages; confirm blocks deletion when messages changed since scan and returns a new summary |
| `DiscordRestApiTests` | Channel listing filters voice channels; person search returns correct fields; message scanning reads only requested channels (not others); deletion uses correct channel endpoint; multi-page scanning works correctly with pagination cursor |
| `McpServerTests` | Server exposes exactly four tools: `list_channels`, `find_people`, `delete_messages`, `confirm_delete_messages` |

**test_safety_regressions.py (safety-specific tests)**

| Test class | Tests |
|-----------|-------|
| `ConstructionTests` | DeleteService can be instantiated before any event loop starts (important for Hermes lifecycle) |
| `ConfirmationSafetyTests` | Preview caps confirmation plans at max_plans limit; expired plans are removed on next call; confirm keeps the plan alive if rescan fails with an exception; changed messages return a reason string and fresh confirmation ID; scan_limit_reached flag propagates to summary even when filtering zeroes the result set |
| `DiscordApiSafetyTests` | Message-by-ID scanning reads directly without paginating through message lists; channel listing includes active threads but excludes categories (type 4) and stage channels (type 13); rate-limit retry works with backoff; bulk-delete is only used for recent messages (within 14 days), older ones are deleted individually |

## Step 7 — Verify the server connects to Discord

To confirm the server actually talks to Discord:

1. Start the server (`python server.py`).
2. In a Hermes session that has this MCP configured, ask it to run `list_channels`.
3. The response should contain channel objects with `id` and `name` fields for text channels, announcement channels, and active threads.

If the bot token or guild ID is wrong, Discord returns 401/403 errors that propagate through the MCP layer. If the network is unreachable, `httpx.AsyncClient` times out after its default timeout (30 seconds per request).

## Environment variables summary

Only these two variables are used:

| Variable | Required? | Used by | What it does |
|----------|-----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | Yes | `server.py::main()` | Authenticates HTTP requests to the Discord API v10 |
| `DISCORD_GUILD_ID` | Yes | `server.py::main()` → `DiscordRestApi.__init__()` | Restricts all operations to one Discord server |

## Troubleshooting

### Server won't start

- Check that both environment variables are set: `echo "$DISCORD_BOT_TOKEN" $DISCORD_GUILD_ID`
- The server exits immediately with a message like `"DISCORD_BOT_TOKEN is required"` if either is blank.

### MCP tools show nothing or error out

- Verify the Hermes profile configuration has an MCP entry named `discord-message-manager`.
- Start a fresh Hermes session after any config change; existing sessions keep their old MCP server processes.
- Check that the bot has permissions: Read Messages, Send Messages (for reply content), and Delete Messages in the target guild.

### Confirm fails with "invalid_confirmation"

- The confirmation ID expired (5 minutes). Run `delete_messages` again to get a fresh one.
- The MCP server process was restarted between preview and confirm, losing all stored plans. Restart and try again.

### Bulk-delete returns failures for old messages

- This is expected behavior. Discord's bulk-delete endpoint does not support messages older than 14 days. These fall through to individual deletion automatically. The summary still reports total `deleted` count (individual deletes succeed within Discord's own rules).
