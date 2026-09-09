# Discord Message Manager for Hermes

This MCP server lets Hermes find and delete messages in one Discord server.

## Tools

- `list_channels`: lists text channels and their IDs.
- `find_people`: finds people and their IDs.
- `delete_messages`: finds matching messages and returns a summary. It never deletes.
- `confirm_delete_messages`: checks the same request again and deletes only after clear confirmation.

## Safety

- The server is fixed to one Discord server ID.
- A blank delete request is rejected.
- Hermes must show the summary before asking for confirmation.
- A confirmation works only for the exact set of messages in the summary and expires after five minutes.
- At most 100 pending confirmations are kept.
- If matching messages change, the server returns a new summary instead of deleting.
- “All channels” covers text channels, announcement channels, and active threads that the bot can read.
- Discord rate limits are followed automatically.
- Recent messages are deleted in safe Discord batches; older messages are deleted one at a time.
- `scan_limit_reached: true` means the request may match more messages than the summary shows. Increase `scan_limit` and create a new summary.

## Supported choices

A delete request can select:

- message IDs
- channel IDs
- person IDs
- all channels
- all people
- channels or people to exclude
- messages before or after a time
- messages containing text

Times use ISO 8601, such as `2026-09-10T12:30:00+10:00`.

## Tests

```bash
/Users/falcon/.hermes/hermes-agent/venv/bin/python -m unittest discover -s tests -v
```

The MCP entry is installed as `discord-message-manager` in the current Hermes profile. Start a new Hermes session after changing its configuration.
