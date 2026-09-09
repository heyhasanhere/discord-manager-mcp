from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from secrets import token_urlsafe
from typing import Any

import httpx


@dataclass(frozen=True)
class DeleteRequest:
    channel_ids: tuple[str, ...] = ()
    author_ids: tuple[str, ...] = ()
    all_channels: bool = False
    all_people: bool = False
    exclude_channel_ids: tuple[str, ...] = ()
    exclude_author_ids: tuple[str, ...] = ()
    message_ids: tuple[str, ...] = ()
    after: str | None = None
    before: str | None = None
    contains: str | None = None
    limit: int = 1000


def filter_messages(request: DeleteRequest, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    for item in messages:
        channel_id = str(item["channel_id"])
        author_id = str(item["author_id"])
        if request.message_ids and str(item["id"]) not in request.message_ids:
            continue
        if request.channel_ids and channel_id not in request.channel_ids:
            continue
        if request.author_ids and author_id not in request.author_ids:
            continue
        if channel_id in request.exclude_channel_ids:
            continue
        if author_id in request.exclude_author_ids:
            continue
        if request.after and datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")) <= datetime.fromisoformat(request.after.replace("Z", "+00:00")):
            continue
        if request.before and datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")) >= datetime.fromisoformat(request.before.replace("Z", "+00:00")):
            continue
        if request.contains and request.contains.casefold() not in item.get("content", "").casefold():
            continue
        selected.append(item)
    return selected


def build_summary(request: DeleteRequest, messages: list[dict[str, Any]]) -> dict[str, Any]:
    channels = Counter(f"{item['channel_name']} ({item['channel_id']})" for item in messages)
    people = Counter(f"{item['author_name']} ({item['author_id']})" for item in messages)
    timestamps = sorted(item["timestamp"] for item in messages)
    return {
        "message_count": len(messages),
        "channels": dict(channels),
        "people": dict(people),
        "excluded_channel_ids": list(request.exclude_channel_ids),
        "excluded_author_ids": list(request.exclude_author_ids),
        "oldest_message": timestamps[0] if timestamps else None,
        "newest_message": timestamps[-1] if timestamps else None,
        "scan_limit": request.limit,
        "scan_limit_reached": len(messages) >= request.limit,
    }


class DiscordRestApi:
    def __init__(self, token: str, guild_id: str, client: httpx.AsyncClient | None = None):
        self.guild_id = str(guild_id)
        self.client = client or httpx.AsyncClient(
            base_url="https://discord.com/api/v10",
            headers={"Authorization": f"Bot {token}", "User-Agent": "HermesDiscordManager/1.0"},
            timeout=30,
        )
        self._owns_client = client is None

    async def list_channels(self) -> list[dict[str, str]]:
        response = await self.client.get(f"/guilds/{self.guild_id}/channels")
        response.raise_for_status()
        return [
            {"id": str(item["id"]), "name": item["name"]}
            for item in response.json()
            if item.get("type") in (0, 5)
        ]

    async def find_people(self, query: str, limit: int = 25) -> list[dict[str, str]]:
        response = await self.client.get(
            f"/guilds/{self.guild_id}/members/search",
            params={"query": query, "limit": min(max(limit, 1), 1000)},
        )
        response.raise_for_status()
        people = []
        for member in response.json():
            user = member["user"]
            people.append({
                "id": str(user["id"]),
                "username": user["username"],
                "display_name": member.get("nick") or user.get("global_name") or user["username"],
            })
        return people

    async def scan_messages(self, request: DeleteRequest) -> list[dict[str, Any]]:
        channels = await self.list_channels()
        if request.channel_ids:
            selected = set(request.channel_ids)
            channels = [item for item in channels if str(item["id"]) in selected]
        messages: list[dict[str, Any]] = []
        for channel in channels:
            before_id: str | None = None
            while len(messages) < request.limit:
                page_limit = min(100, request.limit - len(messages))
                params: dict[str, Any] = {"limit": page_limit}
                if before_id:
                    params["before"] = before_id
                response = await self.client.get(f"/channels/{channel['id']}/messages", params=params)
                response.raise_for_status()
                page = response.json()
                for item in page:
                    author = item["author"]
                    messages.append({
                        "id": str(item["id"]),
                        "channel_id": str(channel["id"]),
                        "channel_name": channel["name"],
                        "author_id": str(author["id"]),
                        "author_name": author.get("global_name") or author.get("username") or str(author["id"]),
                        "content": item.get("content", ""),
                        "timestamp": item["timestamp"],
                    })
                if len(page) < page_limit:
                    break
                before_id = str(page[-1]["id"])
        return messages[:request.limit]

    async def delete_messages(self, messages: list[dict[str, Any]]) -> dict[str, int]:
        deleted = 0
        failed = 0
        for item in messages:
            response = await self.client.delete(f"/channels/{item['channel_id']}/messages/{item['id']}")
            if response.status_code == 204:
                deleted += 1
            else:
                failed += 1
        return {"deleted": deleted, "failed": failed}

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


class DeleteService:
    def __init__(self, api: Any):
        self.api = api
        self.plans: dict[str, dict[str, Any]] = {}

    async def preview(self, request: DeleteRequest) -> dict[str, Any]:
        has_scope = bool(request.message_ids or request.channel_ids or request.author_ids)
        explicit_everything = request.all_channels and request.all_people
        if not has_scope and not explicit_everything:
            return {
                "status": "invalid_request",
                "error": "Choose messages, channels, or people. To select everything, set all_channels and all_people.",
            }
        messages = filter_messages(request, await self.api.scan_messages(request))
        return self._make_plan(request, messages)

    def _make_plan(self, request: DeleteRequest, messages: list[dict[str, Any]]) -> dict[str, Any]:
        confirmation_id = token_urlsafe(18)
        self.plans[confirmation_id] = {"request": request, "messages": messages}
        return {
            "status": "confirmation_required",
            "confirmation_id": confirmation_id,
            "summary": build_summary(request, messages),
            "instruction": "Ask the user to confirm this exact summary. Do not delete yet.",
        }

    async def confirm(self, confirmation_id: str) -> dict[str, Any]:
        plan = self.plans.pop(confirmation_id, None)
        if plan is None:
            return {"status": "invalid_confirmation"}
        request = plan["request"]
        current = filter_messages(request, await self.api.scan_messages(request))
        previewed_ids = {item["id"] for item in plan["messages"]}
        current_ids = {item["id"] for item in current}
        if current_ids != previewed_ids:
            return self._make_plan(request, current)
        result = await self.api.delete_messages(current)
        return {"status": "completed", **result}
