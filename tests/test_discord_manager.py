import unittest

import httpx

from discord_manager import DeleteRequest, DeleteService, DiscordRestApi, build_summary, filter_messages
from server import create_server


class BuildSummaryTests(unittest.TestCase):
    def test_summary_counts_messages_by_channel_and_author(self):
        request = DeleteRequest(channel_ids=("10",), author_ids=())
        messages = [
            {"id": "1", "channel_id": "10", "channel_name": "general", "author_id": "20", "author_name": "alice", "timestamp": "2026-09-10T01:00:00+00:00"},
            {"id": "2", "channel_id": "10", "channel_name": "general", "author_id": "21", "author_name": "bob", "timestamp": "2026-09-10T02:00:00+00:00"},
        ]

        summary = build_summary(request, messages)

        self.assertEqual(summary["message_count"], 2)
        self.assertEqual(summary["channels"], {"general (10)": 2})
        self.assertEqual(summary["people"], {"alice (20)": 1, "bob (21)": 1})
        self.assertEqual(summary["oldest_message"], "2026-09-10T01:00:00+00:00")
        self.assertEqual(summary["newest_message"], "2026-09-10T02:00:00+00:00")
        self.assertEqual(summary["scan_limit"], 1000)
        self.assertFalse(summary["scan_limit_reached"])


class FilterMessagesTests(unittest.TestCase):
    def test_filters_people_channels_and_exclusions(self):
        request = DeleteRequest(
            channel_ids=("10", "11"),
            author_ids=("20", "21"),
            exclude_channel_ids=("11",),
            exclude_author_ids=("21",),
        )
        messages = [
            {"id": "1", "channel_id": "10", "author_id": "20", "content": "keep"},
            {"id": "2", "channel_id": "11", "author_id": "20", "content": "excluded channel"},
            {"id": "3", "channel_id": "10", "author_id": "21", "content": "excluded person"},
            {"id": "4", "channel_id": "12", "author_id": "20", "content": "wrong channel"},
        ]

        result = filter_messages(request, messages)

        self.assertEqual([item["id"] for item in result], ["1"])

    def test_filters_by_message_id(self):
        request = DeleteRequest(message_ids=("2",))
        messages = [
            {"id": "1", "channel_id": "10", "author_id": "20", "content": "one"},
            {"id": "2", "channel_id": "10", "author_id": "20", "content": "two"},
        ]

        result = filter_messages(request, messages)

        self.assertEqual([item["id"] for item in result], ["2"])

    def test_filters_by_time_range(self):
        request = DeleteRequest(after="2026-09-10T01:30:00+00:00", before="2026-09-10T03:00:00+00:00")
        messages = [
            {"id": "1", "channel_id": "10", "author_id": "20", "content": "one", "timestamp": "2026-09-10T01:00:00+00:00"},
            {"id": "2", "channel_id": "10", "author_id": "20", "content": "two", "timestamp": "2026-09-10T02:00:00+00:00"},
            {"id": "3", "channel_id": "10", "author_id": "20", "content": "three", "timestamp": "2026-09-10T03:00:00+00:00"},
        ]

        result = filter_messages(request, messages)

        self.assertEqual([item["id"] for item in result], ["2"])

    def test_filters_by_text(self):
        request = DeleteRequest(contains="SPAM")
        messages = [
            {"id": "1", "channel_id": "10", "author_id": "20", "content": "normal"},
            {"id": "2", "channel_id": "10", "author_id": "20", "content": "Some spam here"},
        ]

        result = filter_messages(request, messages)

        self.assertEqual([item["id"] for item in result], ["2"])


class FakeDiscordApi:
    def __init__(self, scans):
        self.scans = list(scans)
        self.deleted = []

    async def scan_messages(self, request):
        return self.scans.pop(0)

    async def delete_messages(self, messages):
        self.deleted.extend(item["id"] for item in messages)
        return {"deleted": len(messages), "failed": 0}


class DeleteServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_rejects_an_unclear_request(self):
        api = FakeDiscordApi([])
        service = DeleteService(api)

        result = await service.preview(DeleteRequest())

        self.assertEqual(result["status"], "invalid_request")
        self.assertEqual(api.deleted, [])

    async def test_preview_does_not_delete_messages(self):
        messages = [{
            "id": "1", "channel_id": "10", "channel_name": "general",
            "author_id": "20", "author_name": "alice",
            "content": "hello", "timestamp": "2026-09-10T01:00:00+00:00",
        }]
        api = FakeDiscordApi([messages])
        service = DeleteService(api)

        result = await service.preview(DeleteRequest(channel_ids=("10",)))

        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(result["summary"]["message_count"], 1)
        self.assertTrue(result["confirmation_id"])
        self.assertEqual(api.deleted, [])

    async def test_confirm_deletes_only_the_previewed_messages(self):
        messages = [{
            "id": "1", "channel_id": "10", "channel_name": "general",
            "author_id": "20", "author_name": "alice",
            "content": "hello", "timestamp": "2026-09-10T01:00:00+00:00",
        }]
        api = FakeDiscordApi([messages, messages])
        service = DeleteService(api)
        preview = await service.preview(DeleteRequest(channel_ids=("10",)))

        result = await service.confirm(preview["confirmation_id"])

        self.assertEqual(result, {"status": "completed", "deleted": 1, "failed": 0})
        self.assertEqual(api.deleted, ["1"])

    async def test_confirm_stops_when_matching_messages_changed(self):
        first = [{
            "id": "1", "channel_id": "10", "channel_name": "general",
            "author_id": "20", "author_name": "alice",
            "content": "hello", "timestamp": "2026-09-10T01:00:00+00:00",
        }]
        changed = first + [{
            "id": "2", "channel_id": "10", "channel_name": "general",
            "author_id": "20", "author_name": "alice",
            "content": "new", "timestamp": "2026-09-10T02:00:00+00:00",
        }]
        api = FakeDiscordApi([first, changed])
        service = DeleteService(api)
        preview = await service.preview(DeleteRequest(channel_ids=("10",)))

        result = await service.confirm(preview["confirmation_id"])

        self.assertEqual(result["status"], "confirmation_required")
        self.assertEqual(result["summary"]["message_count"], 2)
        self.assertNotEqual(result["confirmation_id"], preview["confirmation_id"])
        self.assertEqual(api.deleted, [])


class DiscordRestApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_channels_returns_message_channels(self):
        def handler(request):
            if request.url.path.endswith("/threads/active"):
                return httpx.Response(200, json={"threads": []})
            return httpx.Response(200, json=[
                {"id": "10", "name": "general", "type": 0},
                {"id": "11", "name": "voice", "type": 2},
            ])

        client = httpx.AsyncClient(base_url="https://discord.com/api/v10", transport=httpx.MockTransport(handler))
        api = DiscordRestApi("token", "g1", client=client)
        try:
            result = await api.list_channels()
        finally:
            await client.aclose()

        self.assertEqual(result, [{"id": "10", "name": "general"}])

    async def test_find_people_returns_ids_and_names(self):
        def handler(request):
            self.assertEqual(request.url.path, "/api/v10/guilds/g1/members/search")
            return httpx.Response(200, json=[{
                "nick": "Ali",
                "user": {"id": "20", "username": "alice", "global_name": "Alice Smith"},
            }])

        client = httpx.AsyncClient(base_url="https://discord.com/api/v10", transport=httpx.MockTransport(handler))
        api = DiscordRestApi("token", "g1", client=client)
        try:
            result = await api.find_people("Ali")
        finally:
            await client.aclose()

        self.assertEqual(result, [{"id": "20", "username": "alice", "display_name": "Ali"}])

    async def test_scan_reads_only_selected_text_channel_from_configured_server(self):
        requested_paths = []

        def handler(request):
            requested_paths.append(request.url.path)
            if request.url.path == "/api/v10/guilds/g1/channels":
                return httpx.Response(200, json=[
                    {"id": "10", "name": "general", "type": 0},
                    {"id": "11", "name": "voice", "type": 2},
                    {"id": "12", "name": "random", "type": 0},
                ])
            if request.url.path == "/api/v10/guilds/g1/threads/active":
                return httpx.Response(200, json={"threads": []})
            if request.url.path == "/api/v10/channels/10/messages":
                return httpx.Response(200, json=[{
                    "id": "1", "channel_id": "10", "content": "hello",
                    "timestamp": "2026-09-10T01:00:00+00:00",
                    "author": {"id": "20", "username": "alice", "global_name": "Alice"},
                }])
            raise AssertionError(f"Unexpected request: {request.url}")

        client = httpx.AsyncClient(
            base_url="https://discord.com/api/v10",
            transport=httpx.MockTransport(handler),
        )
        api = DiscordRestApi("token", "g1", client=client)
        try:
            messages, _ = await api.scan_messages(DeleteRequest(channel_ids=("10",)))
        finally:
            await client.aclose()

        self.assertEqual([item["id"] for item in messages], ["1"])
        self.assertEqual(messages[0]["channel_name"], "general")
        self.assertNotIn("/api/v10/channels/12/messages", requested_paths)

    async def test_delete_uses_the_message_channel(self):
        requested_paths = []

        def handler(request):
            requested_paths.append(request.url.path)
            return httpx.Response(204)

        client = httpx.AsyncClient(
            base_url="https://discord.com/api/v10",
            transport=httpx.MockTransport(handler),
        )
        api = DiscordRestApi("token", "g1", client=client)
        try:
            result = await api.delete_messages([{"id": "1", "channel_id": "10"}])
        finally:
            await client.aclose()

        self.assertEqual(result, {"deleted": 1, "failed": 0, "details": []})
        self.assertEqual(requested_paths, ["/api/v10/channels/10/messages/1"])

    async def test_scan_reads_more_than_one_page(self):
        page_calls = 0

        def handler(request):
            nonlocal page_calls
            if request.url.path == "/api/v10/guilds/g1/channels":
                return httpx.Response(200, json=[{"id": "10", "name": "general", "type": 0}])
            if request.url.path == "/api/v10/guilds/g1/threads/active":
                return httpx.Response(200, json={"threads": []})
            if request.url.path == "/api/v10/channels/10/messages":
                page_calls += 1
                if page_calls == 1:
                    ids = range(1000, 900, -1)
                else:
                    ids = [900]
                return httpx.Response(200, json=[{
                    "id": str(message_id), "channel_id": "10", "content": "x",
                    "timestamp": "2026-09-10T01:00:00+00:00",
                    "author": {"id": "20", "username": "alice"},
                } for message_id in ids])
            raise AssertionError(f"Unexpected request: {request.url}")

        client = httpx.AsyncClient(base_url="https://discord.com/api/v10", transport=httpx.MockTransport(handler))
        api = DiscordRestApi("token", "g1", client=client)
        try:
            messages, _ = await api.scan_messages(DeleteRequest(channel_ids=("10",), limit=150))
        finally:
            await client.aclose()

        self.assertEqual(len(messages), 101)
        self.assertEqual(page_calls, 2)


class McpServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_exposes_discord_tools(self):
        server = create_server(FakeDiscordApi([]))

        tools = await server.list_tools()

        self.assertEqual(
            {tool.name for tool in tools},
            {"list_channels", "find_people", "delete_messages", "confirm_delete_messages"},
        )


if __name__ == "__main__":
    unittest.main()
