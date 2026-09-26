"""Exercise the production callback and HTTP retry against an expired token."""
import ast
import asyncio
import json
import logging
import types
import unittest
import urllib.parse
from pathlib import Path


def production_methods():
    source = Path(__file__).parents[1] / 'src/main.py'
    cls = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'ClawChatHermesBridge')
    names = {'_post_native_control', '_post_hermes_provision_result', '_flush_provision_callback_outbox', '_agent_sync_loop'}
    methods = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    scope = dict(Any=object, urllib=urllib, asyncio=asyncio, logger=logging.getLogger('test'), _safe_agent_sync_error_code=lambda _: 'test')
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(source), 'exec'), scope)
    return {name: scope[name] for name in names}


class ProvisionCallbackAuth(unittest.IsolatedAsyncioTestCase):
    def fixture(self, statuses):
        calls = []
        class Response:
            def __init__(self, status): self.status = status
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def text(self): return '{}'
            async def json(self): return {'success': True}
            def raise_for_status(self):
                if self.status >= 400: raise RuntimeError('HTTP ' + str(self.status))
        class Session:
            def post(self, url, *, json, headers):
                calls.append((url, dict(json), headers['Authorization']))
                return Response(statuses.pop(0))
        class Bridge:
            config = types.SimpleNamespace(api_url='https://relay.example')
            session = Session()
            access_token = 'expired'
            ws = None
            refreshes = 0
            persisted = 0
            async def _authenticate_device(self, session):
                self.refreshes += 1
                return {'tokens': {'accessToken': 'fresh'}}
            def _persist_provision_callback_outbox(self): self.persisted += 1
        for name, method in production_methods().items(): setattr(Bridge, name, method)
        bridge = Bridge()
        bridge._native_http_lock = asyncio.Lock()
        bridge._provision_callback_outbox_lock = asyncio.Lock()
        bridge._native_reconnect_required = False
        bridge._provision_callback_outbox = {'agent:key:complete': {'saved': True}, 'agent:key:fail': {'saved': True}}
        return bridge, calls

    async def test_expired_completion_reauthenticates_and_replays_only_saved_receipt(self):
        bridge, calls = self.fixture([401, 200])
        payload = {'idempotencyKey': 'key', 'runtimeHostId': 'host', 'externalAgentId': 'profile:test'}
        await bridge._post_hermes_provision_result('agent', payload, failed=False)
        self.assertEqual(bridge.refreshes, 1)
        self.assertEqual([c[1] for c in calls], [payload, payload])
        self.assertEqual([c[2] for c in calls], ['Bearer expired', 'Bearer fresh'])
        self.assertNotIn('agent:key:complete', bridge._provision_callback_outbox)
        self.assertTrue(bridge._native_reconnect_required)

    async def test_expired_failure_callback_uses_same_recovery(self):
        bridge, calls = self.fixture([401, 200])
        await bridge._post_hermes_provision_result('agent', {'idempotencyKey': 'key', 'error': 'NATIVE_FAILURE'}, failed=True)
        self.assertEqual(bridge.refreshes, 1)
        self.assertTrue(all(c[0].endswith('/agent/fail') for c in calls))
        self.assertNotIn('agent:key:fail', bridge._provision_callback_outbox)

    async def test_server_failure_keeps_durable_receipt_and_does_not_retry(self):
        bridge, calls = self.fixture([503])
        with self.assertRaises(RuntimeError):
            await bridge._post_hermes_provision_result('agent', {'idempotencyKey': 'key'}, failed=False)
        self.assertIn('agent:key:complete', bridge._provision_callback_outbox)
        self.assertEqual((len(calls), bridge.refreshes, bridge.persisted), (1, 0, 0))

    async def test_second_auth_failure_is_not_retried_forever(self):
        bridge, calls = self.fixture([401, 401])
        with self.assertRaises(RuntimeError):
            await bridge._post_hermes_provision_result('agent', {'idempotencyKey': 'key'}, failed=False)
        self.assertIn('agent:key:complete', bridge._provision_callback_outbox)
        self.assertEqual((len(calls), bridge.refreshes), (2, 1))

    async def test_each_later_expiry_renews_again(self):
        bridge, calls = self.fixture([401, 200, 401, 200, 401, 200])
        for n in range(3):
            key = f"request-{n}"
            bridge._provision_callback_outbox[f"agent:{key}:complete"] = {'saved': True}
            await bridge._post_hermes_provision_result('agent', {'idempotencyKey': key}, failed=False)
            self.assertNotIn(f"agent:{key}:complete", bridge._provision_callback_outbox)
        self.assertEqual((len(calls), bridge.refreshes), (6, 3))

    async def test_periodic_loop_recovers_queued_result_without_new_creation(self):
        bridge, calls = self.fixture([503, 401, 200])
        payload = {'idempotencyKey': 'key', 'runtimeHostId': 'host'}
        bridge._provision_callback_outbox = {'agent:key:complete': {'agentId': 'agent', 'payload': payload, 'failed': False}}
        with self.assertRaises(RuntimeError):
            await bridge._post_hermes_provision_result('agent', payload, failed=False)
        class Socket:
            closed = False
            async def close(self): self.closed = True
        bridge.ws = Socket()
        bridge._stop = asyncio.Event()
        bridge._agent_sync_wakeup = asyncio.Event()
        bridge._agent_sync_wakeup.set()
        await bridge._agent_sync_loop()
        self.assertEqual(bridge._provision_callback_outbox, {})
        self.assertEqual([c[1] for c in calls], [payload, payload, payload])
        self.assertTrue(bridge.ws.closed)

if __name__ == '__main__': unittest.main()
