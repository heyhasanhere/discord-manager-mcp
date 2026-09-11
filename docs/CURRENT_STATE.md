# Discord Message Manager — Current State

## What this project is

This is an MCP server that lets Hermes find and delete messages in one configured Discord server. It runs as a separate process connected to Hermes through stdio transport.

The server was created with two commits:

| Commit | Date | Message |
|--------|------|---------|
| `7ddc24a` | — | Add Discord message management MCP server |
| `3974cdf` | — | Harden Discord message deletion |

## Files at a glance

```
discord-manager-mcp/
├── discord_manager.py    # Core library (372 lines)
│   ├── DeleteRequest     # Filter parameters dataclass
│   ├── filter_messages() # Applies filters to a list of messages
│   ├── build_summary()   # Creates human-readable summary dict
│   ├── DiscordRestApi    # Discord HTTP client with rate-limit handling
│   └── DeleteService     # Confirmation workflow manager
├── server.py             # MCP server wrapper (93 lines)
│   ├── create_server()   # Registers 4 tools on an MCPServer instance
│   └── main()            # Entry point; reads env vars, starts stdio
├── tests/
│   ├── test_discord_manager.py     # Unit tests for core logic and API (296 lines)
│   └── test_safety_regressions.py  # Safety regression tests (210 lines)
├── README.md
├── .gitignore
└── docs/               # Documentation directory
    ├── CURRENT_STATE.md        # This file
    └── REPRODUCIBILITY.md      # How to set up and verify from scratch
```

## Architecture in one sentence

`server.py` exposes four MCP tools that delegate to `DeleteService`, which uses `DiscordRestApi` to talk to the Discord API v10. All stateful planning happens inside a single `DeleteService` instance with an in-memory confirmation store.

### How data flows for deletion

1. User asks Hermes to delete messages, optionally giving criteria (channel IDs, person names, text content, time range).
2. Hermes calls the MCP tool `delete_messages` with those criteria.
3. The server builds a `DeleteRequest`, scans Discord for matching messages, filters them locally, and returns a summary with `status: "confirmation_required"`. No deletion happens here.
4. Hermes shows the summary to the user (or logs it) and waits for clear approval.
5. If approved, Hermes calls `confirm_delete_messages` with the confirmation ID from step 3.
6. The server rescans Discord with the same request. If messages have changed since the preview, it returns a new summary instead of deleting. If nothing has changed, it deletes them and returns `status: "completed"`.

### How data flows for listing / searching

- `list_channels` calls Discord to get text channels (type 0) and announcement channels (type 5) from the configured guild, plus active threads the bot can read (types 10, 11, 12). Categories (type 4) and voice channels (type 2) are excluded.
- `find_people` calls Discord's guild member search with a text query.

## Tool list

| MCP tool name | Purpose | Deletes anything? |
|---------------|---------|-------------------|
| `list_channels` | Lists all readable channels in the configured server and their IDs | No |
| `find_people` | Finds people (members) by search query, returns ID and display name | No |
| `delete_messages` | Scans Discord with given criteria, returns a summary. Never deletes. | No |
| `confirm_delete_messages` | Deletes messages from a previous `delete_messages` summary. Requires `confirmed: true`. | Yes, only after confirmation |

## Delete request filters

A single call to `delete_messages` can combine these filter types. All are optional except at least one must be specified (empty requests are rejected).

| Parameter | Type | What it does |
|-----------|------|-------------|
| `channel_ids` | list of strings | Only messages from these channels |
| `author_ids` | list of strings | Only messages from these people |
| `message_ids` | list of strings | Only these exact message IDs (reads them directly, no paginated scan) |
| `all_channels` | bool | Scan text channels + announcement channels + active threads |
| `all_people` | bool | Include all authors in the scan |
| `exclude_channel_ids` | list of strings | Remove these channels from the result set |
| `exclude_author_ids` | list of strings | Remove these people from the result set |
| `after` | ISO 8601 string | Only messages newer than this time |
| `before` | ISO 8601 string | Only messages older than this time |
| `contains` | string | Only messages whose text contains this substring (case-insensitive) |
| `scan_limit` | int (default 1000, max 100000) | Maximum raw pages to fetch from Discord before returning the summary |

Times use ISO 8601 format, for example: `2026-09-10T12:30:00+10:00`.

## Safety mechanisms

### Two-step confirmation workflow

Deletion is split across two separate tool calls. The first call (`delete_messages`) only produces a summary. The second call (`confirm_delete_messages`) performs the actual deletion. Hermes must show the summary and get explicit user approval before calling the second tool.

### Confirmation IDs are one-time use

Each preview generates a random confirmation ID (18 characters, URL-safe). Once `confirm_delete_messages` is called — whether with `confirmed: true` or `confirmed: false` — that ID is consumed and removed from the server's store. A new preview produces a fresh ID.

### Rescan before deletion

Before deleting anything on confirm, the server rescans Discord with the same request parameters. It compares the set of message IDs from the new scan against the set from the original preview. If they differ by even one message (a new message arrived, someone edited or deleted something), deletion is blocked and a fresh summary is returned with a reason string explaining what changed.

### Confirmation expiry

Confirmations expire after 5 minutes (300 seconds). Expired entries are lazily removed during the next preview or confirm call. If a user tries to confirm an expired ID, the server returns `status: "invalid_confirmation"`.

### Maximum pending confirmations

The server keeps at most 100 active confirmation plans. When this limit is reached, the oldest plan is dropped (based on creation time) to make room for the new one. This prevents unbounded memory growth in long-running sessions.

### Empty request rejection

A `delete_messages` call with no channel IDs, author IDs, message IDs, and neither `all_channels` nor `all_people` set returns an error: `"Choose messages, channels, or people."` Nothing is scanned; nothing is stored.

### Single guild restriction

The server is bound to one Discord server at startup via the `DISCORD_GUILD_ID` environment variable. All tool calls operate on that same guild. Cross-server operations are not possible.

### Rate limit handling

When Discord returns a 429 response, the client retries up to 5 times, respecting the `Retry-After` header or falling back to exponential backoff starting at 1 second and capping at 30 seconds. After 5 failed attempts with 429s, the last response is returned as-is.

### Deletion strategy by message age

Messages older than 14 days (based on Discord's own rules) are deleted one at a time via `DELETE /channels/{id}/messages/{id}`. Messages within 14 days of the current server time may be bulk-deleted in groups of up to 100 using the Discord bulk-delete endpoint (`POST /channels/{id}/messages/bulk-delete`). Bulk delete is never used for messages older than 14 days or when a batch has only one message. If a bulk-delete request fails, those messages fall through to individual deletion as a safety fallback.

### Scan limit warning

If the scan reaches its `scan_limit` before finding all matching messages, the summary includes `"scan_limit_reached": true`. The user should increase `scan_limit`, run `delete_messages` again with the higher value, and work from the new summary. The confirmation ID from a limited scan is not safe to confirm because it does not cover all matches.

## Error states returned by confirm_delete_messages

| Status | When it happens |
|--------|-----------------|
| `"not_confirmed"` | `confirmed` was passed as `false` — nothing was deleted |
| `"invalid_confirmation"` | The confirmation ID is unknown or expired |
| `"confirmation_required"` | Messages changed since preview; a new summary with a fresh ID is returned |
| `"completed"` | All matching messages were successfully deleted (or attempted); the response includes `deleted` and `failed` counts |

## Error states returned by delete_messages

| Status | When it happens |
|--------|-----------------|
| `"confirmation_required"` | Preview succeeded; summary included in response |
| `"invalid_request"` | No selection criteria were provided (empty request) |
| Any unhandled exception during scanning is propagated up through the MCP server |

## Testing

Two test files exist under `tests/`:

- **`test_discord_manager.py`** — Tests for summary building, message filtering (by channel, author, exclusion lists, exact ID, time range, text content), service preview and confirm workflows (rejection of empty requests, no-deletion on preview, successful deletion on confirm, rescan-change detection), Discord REST API calls (channel listing with thread inclusion, person search, paginated scanning), and MCP server tool registration.

- **`test_safety_regressions.py`** — Tests for confirmation plan cap enforcement (101 previews with max_plans=100 drops the oldest), expired plan removal (TTL-based cleanup), plan retention on rescan failure (exception during confirm does not delete the plan), changed-message detection at confirm time, and scan limit propagation to summary. Also covers Discord API safety: direct message-by-ID reads instead of paginated scans for ID-only requests, thread type filtering (includes active threads, excludes stage channels with type 13 and categories with type 4), rate-limit retry behavior, and bulk-delete being restricted to recent messages only.

Run tests from the project root:

```bash
/Users/falcon/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -v
```
