"""Exercise the actual websocket authentication handler without loading Hermes."""
import ast
import asyncio
import json
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

class ControlSubscriptionTests(unittest.TestCase):
    def test_authenticated_bridge_subscribes_before_inventory(self):
        source = Path(__file__).parents[1] / 'src' / 'main.py'
        tree = ast.parse(source.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ClawChatHermesBridge')
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_handle_ws_text')
        namespace = dict(json=json, logger=logging.getLogger('test'), BRIDGE_CAPABILITIES=['clawchat.agent_files.v1'], MARKETPLACE_LOCAL_APP_AGENT_API_REQUEST_CAPABILITY='app')
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
        order = []
        async def send(message): order.append(message)
        async def exchange():
            order.append('inventory')
            return []
        bridge = SimpleNamespace(config=SimpleNamespace(workspace_id='old-workspace'), _send_raw=send,
            _exchange_agent_replicas=exchange, _start_terminal_retry_task=Mock(), _start_agent_sync_task=Mock(),
            _flush_native_replies=AsyncMock(), _flush_terminal_outbox=AsyncMock(), _run_reconnect_backfill=AsyncMock())
        asyncio.run(namespace['_handle_ws_text'](bridge,json.dumps({'type':'authenticated','data':{'workspaceId':'workspace-1'}})))
        self.assertEqual(order[0], {'type':'subscribe_bridge_control','workspaceId':'workspace-1','capabilities':['clawchat.agent_files.v1']})
        self.assertEqual(order[1], 'inventory')

class TerminalReconnectTests(unittest.TestCase):
    def test_reconnect_retries_the_same_exhausted_receipt(self):
        source = Path(__file__).parents[1] / 'src' / 'main.py'
        tree = ast.parse(source.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ClawChatHermesBridge')
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_flush_terminal_outbox')
        namespace = dict(logger=logging.getLogger('test'), TERMINAL_EVENT_MAX_ATTEMPTS=2)
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
        async def run():
            pending = SimpleNamespace(acknowledged=False, attempts=2, exhausted_logged=True)
            send = AsyncMock()
            bridge = SimpleNamespace(_terminal_outbox_lock=asyncio.Lock(), _terminal_outbox={'same-event':pending}, _attempt_terminal_event_delivery=send)
            await namespace['_flush_terminal_outbox'](bridge, reason='retry')
            send.assert_not_awaited()
            await namespace['_flush_terminal_outbox'](bridge, reason='reconnect')
            send.assert_awaited_once_with(pending, reason='reconnect')
        asyncio.run(run())

if __name__ == '__main__': unittest.main()
