import json
import unittest
from datetime import datetime, timedelta, timezone

import httpx

from discord_manager import DeleteRequest, DeleteService, DiscordRestApi


MESSAGE = {
    "id": "1",
    "channel_id": "10",
    "channel_name": "general",
    "author_id": "20",
    "author_name": "alice",
    "content": "hello",
    "timestamp": "2026-09-10T01:00:00+00:00",
}


class FakeApi:
    def __init__(self, scans):
        self.scans = list(scans)
        self.deleted = []

    async def scan_messages(self, request):
        value = self.scans.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def delete_messages(self, messages):
        self.deleted.extend(item["id"] for item in messages)
        return {"deleted": len(messages), "failed": 0, "details": []}


class ConstructionTests(unittest.TestCase):
    def test_delete_service_can_be_created_before_event_loop_starts(self):
        service = DeleteService(FakeApi([]))
        self.assertEqual(service.plans, {})


class ConfirmationSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_caps_saved_confirmation_plans(self):
        now = [0.0]
        api = FakeApi([([MESSAGE], False)] * 101)
        service = DeleteService(api, max_plans=100, clock=lambda: now[0])

        first = await service.preview(DeleteRequest(channel_ids=("10",)))
        for index in range(100):
            now[0] = float(index + 1)
            await service.preview(DeleteRequest(channel_ids=("10",)))

        self.assertEqual(len(service.plans), 100)
        self.assertNotIn(first["confirmation_id"], service.plans)

    async def test_preview_removes_expired_confirmation_plans(self):
        now = [0.0]
        api = FakeApi([([MESSAGE], False), ([MESSAGE], False)])
        service = DeleteService(api, confirmation_ttl_seconds=10, clock=lambda: now[0])
        first = await service.preview(DeleteRequest(channel_ids=("10",)))

        now[0] = 11.0
        await service.preview(DeleteRequest(channel_ids=("10",)))

        self.assertNotIn(first["confirmation_id"], service.plans)

    async def test_confirm_keeps_plan_when_rescan_fails(self):
        api = FakeApi([([MESSAGE], False), RuntimeError("network")])
        service = DeleteService(api)
        preview = await service.preview(DeleteRequest(channel_ids=("10",)))

        with self.assertRaises(RuntimeError):
            await service.confirm(preview["confirmation_id"])

        self.assertIn(preview["confirmation_id"], service.plans)

    async def test_changed_messages_return_reason_and_new_confirmation(self):
        changed = [MESSAGE, {**MESSAGE, "id": "2"}]
        api = FakeApi([([MESSAGE], False), (changed, False)])
        service = DeleteService(api)
        preview = await service.preview(DeleteRequest(channel_ids=("10",)))

        result = await service.confirm(preview["confirmation_id"])

        self.assertEqual(result["status"], "confirmation_required")
        self.assertIn("changed", result["reason"].lower())
        self.assertNotEqual(result["confirmation_id"], preview["confirmation_id"])
        self.assertEqual(api.deleted, [])

    async def test_summary_uses_raw_scan_limit_state(self):
        api = FakeApi([([MESSAGE], True)])
        service = DeleteService(api)

        result = await service.preview(DeleteRequest(channel_ids=("10",), author_ids=("999",), limit=1))

        self.assertTrue(result["summary"]["scan_limit_reached"])
        self.assertEqual(result["summary"]["message_count"], 0)


class DiscordApiSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_specific_message_is_read_directly(self):
        requested_paths = []

        def handler(request):
            requested_paths.append(request.url.path)
            if request.url.path.endswith("/guilds/g1/channels"):
                return httpx.Response(200, json=[{"id": "10", "name": "general", "type": 0}])
            if request.url.path.endswith("/guilds/g1/threads/active"):
                return httpx.Response(200, json={"threads": []})
            if request.url.path.endswith("/channels/10/messages/999"):
                return httpx.Response(200, json={
                    "id": "999", "channel_id": "10", "content": "old message",
                    "timestamp": "2020-01-01T00:00:00+00:00",
                    "author": {"id": "20", "username": "alice"},
                })
            raise AssertionError(request.url.path)

        client = httpx.AsyncClient(base_url="https://discord.com/api/v10", transport=httpx.MockTransport(handler))
        try:
            api = DiscordRestApi("token", "g1", client=client)
            messages, limit_reached = await api.scan_messages(
                DeleteRequest(channel_ids=("10",), message_ids=("999",))
            )
        finally:
            await client.aclose()

        self.assertEqual([message["id"] for message in messages], ["999"])
        self.assertFalse(limit_reached)
        self.assertNotIn("/api/v10/channels/10/messages", requested_paths)

    async def test_channels_include_active_threads_but_not_categories(self):
        def handler(request):
            if request.url.path.endswith("/channels"):
                return httpx.Response(200, json=[
                    {"id": "10", "name": "general", "type": 0},
                    {"id": "20", "name": "category", "type": 4},
                ])
            if request.url.path.endswith("/threads/active"):
                return httpx.Response(200, json={"threads": [
                    {"id": "30", "name": "active-thread", "type": 11},
                    {"id": "40", "name": "stage", "type": 13},
                ]})
            raise AssertionError(request.url.path)

        client = httpx.AsyncClient(base_url="https://discord.com/api/v10", transport=httpx.MockTransport(handler))
        try:
            result = await DiscordRestApi("token", "g1", client=client).list_channels()
        finally:
            await client.aclose()

        self.assertEqual(result, [
            {"id": "10", "name": "general"},
            {"id": "30", "name": "active-thread"},
        ])

    async def test_rate_limit_retries_then_succeeds(self):
        calls = 0
        waits = []

        async def fake_sleep(seconds):
            waits.append(seconds)

        def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, json={"retry_after": 0.25})
            return httpx.Response(200, json=[])

        client = httpx.AsyncClient(base_url="https://discord.com/api/v10", transport=httpx.MockTransport(handler))
        try:
            api = DiscordRestApi("token", "g1", client=client, sleep=fake_sleep)
            response = await api._request("GET", "/test")
        finally:
            await client.aclose()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, 2)
        self.assertEqual(waits, [0.25])

    async def test_delete_bulk_is_only_used_for_recent_messages(self):
        requests = []
        now = datetime(2026, 9, 10, tzinfo=timezone.utc)

        def handler(request):
            requests.append((request.method, request.url.path, json.loads(request.content or b"{}")))
            return httpx.Response(204)

        recent = (now - timedelta(days=1)).isoformat()
        old = (now - timedelta(days=20)).isoformat()
        messages = [
            {"id": "1", "channel_id": "10", "timestamp": recent},
            {"id": "2", "channel_id": "10", "timestamp": recent},
            {"id": "3", "channel_id": "10", "timestamp": old},
        ]
        client = httpx.AsyncClient(base_url="https://discord.com/api/v10", transport=httpx.MockTransport(handler))
        try:
            api = DiscordRestApi("token", "g1", client=client, now=lambda: now)
            result = await api.delete_messages(messages)
        finally:
            await client.aclose()

        self.assertEqual(result["deleted"], 3)
        self.assertIn(("POST", "/api/v10/channels/10/messages/bulk-delete", {"messages": ["1", "2"]}), requests)
        self.assertIn(("DELETE", "/api/v10/channels/10/messages/3", {}), requests)


if __name__ == "__main__":
    unittest.main()
