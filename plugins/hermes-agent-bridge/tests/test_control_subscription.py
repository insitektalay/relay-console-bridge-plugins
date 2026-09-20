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
            _flush_terminal_outbox=AsyncMock(), _run_reconnect_backfill=AsyncMock())
        asyncio.run(namespace['_handle_ws_text'](bridge,json.dumps({'type':'authenticated','data':{'workspaceId':'workspace-1'}})))
        self.assertEqual(order[0], {'type':'subscribe_bridge_control','workspaceId':'workspace-1','capabilities':['clawchat.agent_files.v1']})
        self.assertEqual(order[1], 'inventory')

if __name__ == '__main__': unittest.main()
