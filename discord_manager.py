from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from time import monotonic
from typing import Any, Awaitable, Callable

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


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def filter_messages(request: DeleteRequest, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    after = _parse_time(request.after) if request.after else None
    before = _parse_time(request.before) if request.before else None
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
        timestamp = _parse_time(item["timestamp"]) if item.get("timestamp") else None
        if after and timestamp and timestamp <= after:
            continue
        if before and timestamp and timestamp >= before:
            continue
        if request.contains and request.contains.casefold() not in item.get("content", "").casefold():
            continue
        selected.append(item)
    return selected


def build_summary(
    request: DeleteRequest,
    messages: list[dict[str, Any]],
    *,
    scan_limit_reached: bool = False,
) -> dict[str, Any]:
    channels = Counter(f"{item['channel_name']} ({item['channel_id']})" for item in messages)
    people = Counter(f"{item['author_name']} ({item['author_id']})" for item in messages)
    timestamps = sorted(item["timestamp"] for item in messages if item.get("timestamp"))
    return {
        "message_count": len(messages),
        "channels": dict(channels),
        "people": dict(people),
        "excluded_channel_ids": list(request.exclude_channel_ids),
        "excluded_author_ids": list(request.exclude_author_ids),
        "oldest_message": timestamps[0] if timestamps else None,
        "newest_message": timestamps[-1] if timestamps else None,
        "scan_limit": request.limit,
        "scan_limit_reached": scan_limit_reached,
    }


class DiscordRestApi:
    def __init__(
        self,
        token: str,
        guild_id: str,
        client: httpx.AsyncClient | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] | None = None,
    ):
        self.guild_id = str(guild_id)
        self.client = client or httpx.AsyncClient(
            base_url="https://discord.com/api/v10",
            headers={"Authorization": f"Bot {token}", "User-Agent": "HermesDiscordManager/1.0"},
            timeout=30,
        )
        self._owns_client = client is None
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        backoff = 1.0
        for attempt in range(5):
            response = await self.client.request(method, path, **kwargs)
            if response.status_code != 429:
                return response
            if attempt == 4:
                return response
            retry_after: Any = response.headers.get("Retry-After")
            if retry_after is None:
                try:
                    retry_after = response.json().get("retry_after")
                except (ValueError, AttributeError):
                    retry_after = None
            try:
                wait = float(retry_after) if retry_after is not None else backoff
            except (TypeError, ValueError):
                wait = backoff
            await self._sleep(min(max(wait, 0.0), 30.0))
            backoff = min(backoff * 2, 30.0)
        raise RuntimeError("Discord retry loop ended unexpectedly")

    async def list_channels(self) -> list[dict[str, str]]:
        response = await self._request("GET", f"/guilds/{self.guild_id}/channels")
        response.raise_for_status()
        channels = [
            {"id": str(item["id"]), "name": item["name"]}
            for item in response.json()
            if item.get("type") in (0, 5)
        ]

        response = await self._request("GET", f"/guilds/{self.guild_id}/threads/active")
        response.raise_for_status()
        channels.extend(
            {"id": str(item["id"]), "name": item["name"]}
            for item in response.json().get("threads", [])
            if item.get("type") in (10, 11, 12)
        )

        unique: dict[str, dict[str, str]] = {}
        for channel in channels:
            unique[channel["id"]] = channel
        return list(unique.values())

    async def find_people(self, query: str, limit: int = 25) -> list[dict[str, str]]:
        response = await self._request(
            "GET",
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

    @staticmethod
    def _normalise_message(item: dict[str, Any], channel: dict[str, str]) -> dict[str, Any]:
        author = item["author"]
        return {
            "id": str(item["id"]),
            "channel_id": str(channel["id"]),
            "channel_name": channel["name"],
            "author_id": str(author["id"]),
            "author_name": author.get("global_name") or author.get("username") or str(author["id"]),
            "content": item.get("content", ""),
            "timestamp": item["timestamp"],
        }

    async def scan_messages(self, request: DeleteRequest) -> tuple[list[dict[str, Any]], bool]:
        channels = await self.list_channels()
        if request.channel_ids:
            selected = set(request.channel_ids)
            channels = [item for item in channels if item["id"] in selected]

        if request.message_ids and len(channels) == 1:
            channel = channels[0]
            messages = []
            for message_id in request.message_ids[:request.limit]:
                response = await self._request(
                    "GET", f"/channels/{channel['id']}/messages/{message_id}"
                )
                response.raise_for_status()
                messages.append(self._normalise_message(response.json(), channel))
            return messages, len(request.message_ids) > request.limit

        messages: list[dict[str, Any]] = []
        limit_reached = False
        for channel in channels:
            before_id: str | None = None
            while True:
                remaining = request.limit - len(messages)
                if remaining <= 0:
                    limit_reached = True
                    break
                page_limit = min(100, remaining)
                params: dict[str, Any] = {"limit": page_limit}
                if before_id:
                    params["before"] = before_id
                response = await self._request("GET", f"/channels/{channel['id']}/messages", params=params)
                response.raise_for_status()
                page = response.json()
                for item in page:
                    messages.append(self._normalise_message(item, channel))
                if len(page) < page_limit:
                    break
                before_id = str(page[-1]["id"])
            if limit_reached:
                break
        return messages[:request.limit], limit_reached

    def _is_recent(self, message: dict[str, Any]) -> bool:
        timestamp = message.get("timestamp")
        if not timestamp:
            return False
        created = _parse_time(timestamp)
        now = self._now()
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return created > now - timedelta(days=14)

    async def _delete_one(self, channel_id: str, message_id: str) -> tuple[bool, dict[str, str] | None]:
        response = await self._request("DELETE", f"/channels/{channel_id}/messages/{message_id}")
        if response.status_code == 204:
            return True, None
        return False, {
            "channel_id": channel_id,
            "message_id": message_id,
            "status": str(response.status_code),
        }

    async def delete_messages(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        deleted = 0
        details: list[dict[str, str]] = []
        by_channel: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for message in messages:
            by_channel[str(message["channel_id"])].append(message)

        for channel_id, channel_messages in by_channel.items():
            recent = [message for message in channel_messages if self._is_recent(message)]
            individual = [message for message in channel_messages if not self._is_recent(message)]

            for start in range(0, len(recent), 100):
                batch = recent[start:start + 100]
                if len(batch) == 1:
                    individual.extend(batch)
                    continue
                response = await self._request(
                    "POST",
                    f"/channels/{channel_id}/messages/bulk-delete",
                    json={"messages": [message["id"] for message in batch]},
                )
                if response.status_code == 204:
                    deleted += len(batch)
                else:
                    individual.extend(batch)

            for message in individual:
                ok, detail = await self._delete_one(channel_id, str(message["id"]))
                if ok:
                    deleted += 1
                elif detail:
                    details.append(detail)

        return {"deleted": deleted, "failed": len(details), "details": details}

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()


class DeleteService:
    def __init__(
        self,
        api: Any,
        *,
        confirmation_ttl_seconds: float = 300,
        max_plans: int = 100,
        clock: Callable[[], float] | None = None,
    ):
        self.api = api
        self.plans: dict[str, dict[str, Any]] = {}
        self.confirmation_ttl_seconds = confirmation_ttl_seconds
        self.max_plans = max(1, max_plans)
        self._clock = clock or monotonic

    @staticmethod
    def _scan_parts(result: Any) -> tuple[list[dict[str, Any]], bool]:
        if isinstance(result, tuple):
            return result
        return result, False

    def _remove_expired_plans(self) -> None:
        now = self._clock()
        expired = [
            confirmation_id
            for confirmation_id, plan in self.plans.items()
            if now - plan["created_at"] > self.confirmation_ttl_seconds
        ]
        for confirmation_id in expired:
            self.plans.pop(confirmation_id, None)

    def _make_plan(
        self,
        request: DeleteRequest,
        messages: list[dict[str, Any]],
        scan_limit_reached: bool,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self._remove_expired_plans()
        while len(self.plans) >= self.max_plans:
            oldest = min(self.plans, key=lambda key: self.plans[key]["created_at"])
            self.plans.pop(oldest, None)
        confirmation_id = token_urlsafe(18)
        self.plans[confirmation_id] = {
            "request": request,
            "messages": messages,
            "scan_limit_reached": scan_limit_reached,
            "created_at": self._clock(),
        }
        result = {
            "status": "confirmation_required",
            "confirmation_id": confirmation_id,
            "summary": build_summary(request, messages, scan_limit_reached=scan_limit_reached),
            "instruction": "Show this exact summary and ask the user to confirm. Do not delete yet.",
        }
        if reason:
            result["reason"] = reason
        return result

    async def preview(self, request: DeleteRequest) -> dict[str, Any]:
        has_scope = bool(request.message_ids or request.channel_ids or request.author_ids)
        explicit_everything = request.all_channels and request.all_people
        if not has_scope and not explicit_everything:
            return {
                "status": "invalid_request",
                "error": "Choose messages, channels, or people. To select everything, set all_channels and all_people.",
            }
        self._remove_expired_plans()
        raw_messages, limit_reached = self._scan_parts(await self.api.scan_messages(request))
        messages = filter_messages(request, raw_messages)
        return self._make_plan(request, messages, limit_reached)

    async def confirm(self, confirmation_id: str) -> dict[str, Any]:
        self._remove_expired_plans()
        plan = self.plans.get(confirmation_id)
        if plan is None:
            return {"status": "invalid_confirmation"}

        request = plan["request"]
        raw_messages, limit_reached = self._scan_parts(await self.api.scan_messages(request))
        current = filter_messages(request, raw_messages)
        previewed_ids = {item["id"] for item in plan["messages"]}
        current_ids = {item["id"] for item in current}
        self.plans.pop(confirmation_id, None)

        if current_ids != previewed_ids:
            reason = f"Matching messages changed: the summary had {len(previewed_ids)}, and the new scan found {len(current_ids)}."
            return self._make_plan(request, current, limit_reached, reason=reason)

        result = await self.api.delete_messages(current)
        return {"status": "completed", **result}
