#!/usr/bin/env python3
from __future__ import annotations

import os
from typing import Any

from mcp.server import MCPServer

from discord_manager import DeleteRequest, DeleteService, DiscordRestApi


def create_server(api: Any) -> MCPServer:
    service = DeleteService(api)
    server = MCPServer(
        "discord-message-manager",
        description="Manage messages in one configured Discord server.",
        instructions=(
            "Use delete_messages to show a summary first. Never call "
            "confirm_delete_messages until the user clearly confirms that exact summary."
        ),
    )

    @server.tool(description="List text channels in the configured Discord server.", structured_output=True)
    async def list_channels() -> dict[str, Any]:
        return {"channels": await api.list_channels()}

    @server.tool(description="Find people in the configured Discord server by name.", structured_output=True)
    async def find_people(query: str, limit: int = 25) -> dict[str, Any]:
        return {"people": await api.find_people(query, limit)}

    @server.tool(
        description=(
            "Find messages that match the request and return a deletion summary. "
            "This tool never deletes messages. Show the summary to the user and ask for confirmation."
        ),
        structured_output=True,
    )
    async def delete_messages(
        channel_ids: list[str] | None = None,
        author_ids: list[str] | None = None,
        all_channels: bool = False,
        all_people: bool = False,
        exclude_channel_ids: list[str] | None = None,
        exclude_author_ids: list[str] | None = None,
        message_ids: list[str] | None = None,
        after: str | None = None,
        before: str | None = None,
        contains: str | None = None,
        scan_limit: int = 1000,
    ) -> dict[str, Any]:
        request = DeleteRequest(
            channel_ids=tuple(channel_ids or ()),
            author_ids=tuple(author_ids or ()),
            all_channels=all_channels,
            all_people=all_people,
            exclude_channel_ids=tuple(exclude_channel_ids or ()),
            exclude_author_ids=tuple(exclude_author_ids or ()),
            message_ids=tuple(message_ids or ()),
            after=after,
            before=before,
            contains=contains,
            limit=min(max(scan_limit, 1), 100_000),
        )
        return await service.preview(request)

    @server.tool(
        description=(
            "Delete the messages from a previous delete_messages summary. "
            "Call only after the user clearly confirms that exact summary."
        ),
        structured_output=True,
    )
    async def confirm_delete_messages(confirmation_id: str, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            return {"status": "not_confirmed", "deleted": 0}
        return await service.confirm(confirmation_id)

    return server


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is required")
    if not guild_id:
        raise SystemExit("DISCORD_GUILD_ID is required")
    api = DiscordRestApi(token, guild_id)
    create_server(api).run(transport="stdio")


if __name__ == "__main__":
    main()
