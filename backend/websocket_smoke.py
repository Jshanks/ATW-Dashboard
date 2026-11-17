"""WebSocket contract smoke tests for ATW Dashboard.

Run inside the application image or any environment with the application
requirements installed:

    python -m backend.websocket_smoke
"""

import asyncio
import json
import unittest

from fastapi import WebSocketDisconnect

from backend import main
from backend.models import ConnectionState, ItemState, ItemStatus, WarriorStatus


_LONG_ITEM_NAME = "Item " + ("post:large-channel:12345\0" * 500)


class _FakeClient:
    def __init__(self, status):
        self.status = status


class _EndpointWebSocket:
    def __init__(self):
        self.accepted = False
        self.sent_text = []
        self._received_ping = False

    async def accept(self):
        self.accepted = True

    async def send_text(self, message):
        self.sent_text.append(message)

    async def receive_text(self):
        if not self._received_ping:
            self._received_ping = True
            return json.dumps({"type": "ping"})
        raise WebSocketDisconnect()


class _BroadcastWebSocket:
    def __init__(self):
        self.sent_text = []

    async def send_text(self, message):
        self.sent_text.append(message)


class WebSocketContractSmokeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_clients = main.clients
        self.original_ws_connections = main.ws_connections
        self.original_build_tracker_stats = main._build_tracker_stats
        self.original_get_bucketed = main.history.get_bucketed
        self.original_history_save = main.history.save
        self.original_pause_status = main._build_pause_status
        self.original_sleep = main.asyncio.sleep

        item = ItemStatus(
            item_id="item-7",
            item_name=_LONG_ITEM_NAME,
            state=ItemState.DOWNLOADING,
            task_description="WgetDownload",
        )
        status = WarriorStatus(
            name="warrior-test",
            host="127.0.0.1",
            port=8001,
            url="http://127.0.0.1:8001",
            connection_state=ConnectionState.ONLINE,
            current_project="Telegram",
            project_slug="telegram",
            selected_project="telegram",
            downloader="giggity",
            concurrent_items=1,
            items=[item],
            last_seen="2026-11-17T17:00:00+00:00",
            bandwidth_down=123.5,
            bandwidth_up=456.75,
            bytes_downloaded=123456789,
            bytes_uploaded=987654321,
        )

        main.clients = {status.name: _FakeClient(status)}
        main.ws_connections = []

        async def empty_tracker_stats():
            return {"tracker_stats": []}

        main._build_tracker_stats = empty_tracker_stats
        main.history.get_bucketed = lambda: {
            "interval_minutes": 5,
            "buckets": [],
        }
        main.history.save = lambda: None
        main._build_pause_status = lambda: []

    def tearDown(self):
        main.clients = self.original_clients
        main.ws_connections = self.original_ws_connections
        main._build_tracker_stats = self.original_build_tracker_stats
        main.history.get_bucketed = self.original_get_bucketed
        main.history.save = self.original_history_save
        main._build_pause_status = self.original_pause_status
        main.asyncio.sleep = self.original_sleep

    def assert_compact_state(self, payload):
        self.assertEqual(payload["total_online"], 1)
        self.assertEqual(payload["total_offline"], 0)
        self.assertEqual(payload["total_items_active"], 1)
        self.assertEqual(len(payload["instances"]), 1)

        instance = payload["instances"][0]
        self.assertEqual(instance["name"], "warrior-test")
        self.assertEqual(instance["project_slug"], "telegram")
        self.assertEqual(instance["downloader"], "giggity")
        self.assertEqual(instance["bandwidth_down"], 123.5)
        self.assertEqual(instance["bytes_uploaded"], 987654321)

        self.assertEqual(
            instance["items"],
            [{"item_id": "item-7", "state": "downloading"}],
        )
        self.assertNotIn("item_name", instance["items"][0])
        self.assertNotIn("task_description", instance["items"][0])
        self.assertNotIn("post:large-channel", json.dumps(payload))

    async def test_initial_websocket_frame_and_ping_pong(self):
        websocket = _EndpointWebSocket()

        await main.websocket_endpoint(websocket)

        self.assertTrue(websocket.accepted)
        self.assertEqual(len(websocket.sent_text), 2)
        self.assertNotIn(websocket, main.ws_connections)

        initial_payload = json.loads(websocket.sent_text[0])
        self.assert_compact_state(initial_payload)
        self.assertIn("history", initial_payload)
        self.assertIn("tracker_stats", initial_payload)
        self.assertIn("pause_status", initial_payload)

        self.assertEqual(
            json.loads(websocket.sent_text[1]),
            {"type": "pong"},
        )

    async def test_recurring_broadcast_frame(self):
        websocket = _BroadcastWebSocket()
        main.ws_connections = [websocket]
        sleep_calls = 0

        async def one_iteration_sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise asyncio.CancelledError()

        main.asyncio.sleep = one_iteration_sleep

        await main._broadcast_loop()

        self.assertEqual(len(websocket.sent_text), 1)
        payload = json.loads(websocket.sent_text[0])
        self.assert_compact_state(payload)

    def test_compact_payload_is_materially_smaller(self):
        full_json = main._build_dashboard_state().model_dump_json()
        live_json = main._build_live_dashboard_state().model_dump_json()

        self.assertLess(len(live_json), len(full_json) * 0.25)
        self.assertNotIn("post:large-channel", live_json)

        reduction = round((1 - len(live_json) / len(full_json)) * 100, 1)
        print(
            f"Full frame: {len(full_json):,} bytes; "
            f"live frame: {len(live_json):,} bytes; "
            f"reduction: {reduction}%"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
