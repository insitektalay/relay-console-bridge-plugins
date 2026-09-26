"""An expired sync token must trigger normal, durable device reauthentication."""
import ast
import asyncio
import types
import unittest
from pathlib import Path

source = Path(__file__).parents[1] / 'src/main.py'
cls = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'ClawChatHermesBridge')
method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_exchange_agent_replicas')
scope = {}
exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), scope)

class SyncAuthRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_each_expired_sync_reconnects_instead_of_retrying_expired_token_forever(self):
        class Socket:
            closed = False
            async def close(self): self.closed = True
        class Bridge:
            async def _exchange_agent_replicas_locked(self):
                if self.expired: raise RuntimeError('HERMES_AGENT_SYNC_HTTP_401')
                return ['profile:new']
        bridge = Bridge()
        bridge._native_control_lock = asyncio.Lock()
        for _ in range(3):
            bridge.ws = Socket()
            bridge.expired = True
            with self.assertRaisesRegex(RuntimeError, 'HTTP_401'):
                await scope['_exchange_agent_replicas'](bridge)
            self.assertTrue(bridge.ws.closed, '401 must leave the long-lived socket and enter normal device authentication')
            bridge.ws = Socket()
            bridge.expired = False
            self.assertEqual(await scope['_exchange_agent_replicas'](bridge), ['profile:new'])
            self.assertFalse(bridge.ws.closed)

    async def test_transient_server_error_does_not_rotate_device_credentials(self):
        class Bridge:
            async def _exchange_agent_replicas_locked(self):
                raise RuntimeError('HERMES_AGENT_SYNC_HTTP_503')
        bridge = Bridge()
        bridge.ws = types.SimpleNamespace(closed=False)
        bridge._native_control_lock = asyncio.Lock()
        with self.assertRaisesRegex(RuntimeError, 'HTTP_503'):
            await scope['_exchange_agent_replicas'](bridge)
        self.assertFalse(bridge.ws.closed)
