import importlib.util
import tempfile
import unittest
import uuid
from pathlib import Path

spec = importlib.util.spec_from_file_location('native_files', Path(__file__).parents[1] / 'src' / 'agent_file_store.py')
store = importlib.util.module_from_spec(spec)
spec.loader.exec_module(store)


class AgentFiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / 'agent'
        self.root.mkdir()
        self.state = str(Path(self.temp.name).resolve() / 'bridge')

    def tearDown(self):
        self.temp.cleanup()

    def command(self, action, path, content=None, base=None):
        value = dict(operationId=str(uuid.uuid4()), requestHash=uuid.uuid4().hex * 2,
                     action=action, path=path, expiresAt='2099-01-01T00:00:00Z', _claimAllowed=True)
        if content is not None:
            value.update(content=content, contentHash=store.digest(content))
        if base is not None:
            value['baseVersion'] = base
        return value

    def run_command(self, *args, **kwargs):
        return store.execute(str(self.root), self.state, self.command(*args, **kwargs))

    def test_lists_agent_root_before_any_files_exist(self):
        result = self.run_command('list', '')
        self.assertEqual(result['status'], 'listed')
        self.assertEqual(result['files'], [])
        self.assertEqual(result['folders'], [])

    def test_create_read_edit_and_conflict(self):
        self.assertEqual(self.run_command('create', 'AGENTS.md', '# Instructions', 'absent')['status'], 'applied')
        first = self.run_command('read', 'AGENTS.md')
        self.assertEqual(first['content'], '# Instructions')
        self.assertEqual(self.run_command('write', 'AGENTS.md', '# New', first['version'])['status'], 'applied')
        self.assertEqual(self.run_command('write', 'AGENTS.md', '# Stale', first['version'])['status'], 'conflict')
        self.assertEqual((self.root / 'AGENTS.md').read_text(), '# New')

    def test_create_skill_and_list(self):
        self.run_command('mkdir', 'skills', base='absent')
        self.run_command('mkdir', 'skills/example', base='absent')
        self.run_command('create', 'skills/example/SKILL.md', '# Skill', 'absent')
        self.assertEqual(self.run_command('list', 'skills')['folders'][0]['path'], 'skills/example')
        self.assertEqual(self.run_command('list', 'skills/example')['files'][0]['filename'], 'SKILL.md')

    def test_replay_does_not_repeat_write(self):
        command = self.command('create', 'MEMORY.md', 'first', 'absent')
        first = store.execute(str(self.root), self.state, command)
        (self.root / 'MEMORY.md').write_text('later')
        self.assertEqual(store.execute(str(self.root), self.state, command), first)
        self.assertEqual((self.root / 'MEMORY.md').read_text(), 'later')

    def test_rejects_symlinks_hardlinks_and_escape(self):
        secret = self.root.parent / 'secret.md'
        secret.write_text('private')
        (self.root / 'AGENTS.md').symlink_to(secret)
        (self.root / 'skills').symlink_to(self.root.parent, target_is_directory=True)
        import os
        os.link(secret, self.root / 'MEMORY.md')
        for path in ['AGENTS.md', 'MEMORY.md', 'skills/secret.md', '../secret.md']:
            self.assertNotEqual(self.run_command('read', path)['status'], 'read')
        self.assertEqual(secret.read_text(), 'private')

    def test_unclaimed_or_expired_operations_do_not_write(self):
        command = self.command('create', 'AGENTS.md', 'text', 'absent')
        command['_claimAllowed'] = False
        self.assertTrue(store.execute(str(self.root), self.state, command)['noStart'])
        command['_claimAllowed'] = True
        command['expiresAt'] = '2000-01-01T00:00:00Z'
        self.assertTrue(store.execute(str(self.root), self.state, command)['noStart'])
        self.assertFalse((self.root / 'AGENTS.md').exists())

    def test_reserved_command_is_cancelled_before_start_on_recovery(self):
        command = self.command('create', 'AGENTS.md', 'text', 'absent')
        store.recover(self.state, reserve=command)
        receipt = store.recover(self.state)[0]
        self.assertTrue(receipt['noStart'])
        self.assertEqual(store.execute(str(self.root), self.state, command), receipt)
        self.assertFalse((self.root / 'AGENTS.md').exists())

    def test_receipts_do_not_store_file_contents(self):
        self.run_command('create', 'AGENTS.md', 'private instructions', 'absent')
        self.assertNotIn(b'private instructions', (Path(self.state) / 'agent-file-receipts.sqlite').read_bytes())

    def test_hermes_handler_claims_and_completes_the_native_write(self):
        import ast
        import asyncio
        import logging
        import sys
        import types
        import typing
        source = Path(__file__).parents[1] / 'src' / 'main.py'
        tree = ast.parse(source.read_text())
        bridge_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'ClawChatHermesBridge')
        handler = next(node for node in bridge_class.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'handle_agent_file')
        scope = {'__name__': 'handler_fixture', '__package__': '', 'asyncio': asyncio, 'uuid': uuid, 'Any': typing.Any, 'logger': logging.getLogger('test'), '_config_dir': lambda: Path(self.state)}
        sys.path.insert(0, str(source.parent))
        try:
            exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), 'exec'), scope)
            command = self.command('create', 'AGENTS.md', '# Hermes', 'absent')
            command.update(workspaceId='workspace', runtimeType='hermes', externalAgentId='profile:example', workspaceGeneration='27', requestId='request')
            completions, messages = [], []
            class Response:
                def __init__(self, body): self.body = body
                async def __aenter__(self): return self
                async def __aexit__(self, *_): pass
                def raise_for_status(self): pass
                async def json(self): return self.body
            class Session:
                def post(self, url, *, json, headers):
                    if url.endswith('/claim'):
                        return Response({**command, 'allowed': True})
                    completions.append(json)
                    return Response({'result': json})
            class Bridge:
                handle_agent_file = scope['handle_agent_file']
                config = types.SimpleNamespace(workspace_id='workspace', api_url='https://relay.example')
                access_token = 'fixture-token'
                session = Session()
                def _refresh_native_profiles(_): return {'profile:example': types.SimpleNamespace(home=self.root)}
                async def _post_native_control(self, path, body):
                    async with self.session.post(path, json=body, headers={}) as response:
                        return await response.json()
                async def _send_native_reply(_, message): messages.append(message)
            asyncio.run(Bridge().handle_agent_file(command))
            self.assertEqual(messages[-1]['type'], 'clawchat.agent_file.result')
            self.assertEqual(messages[-1]['data']['status'], 'applied')
            self.assertEqual((self.root / 'AGENTS.md').read_text(), '# Hermes')
            self.assertTrue(completions[-1]['nativeInactive'])
            self.assertNotIn('content', completions[-1])
        finally:
            sys.path.pop(0)

    def test_openclaw_uses_same_store(self):
        hermes = Path(__file__).parents[1] / 'src' / 'agent_file_store.py'
        openclaw = Path(__file__).parents[2] / 'openclaw-bridge' / 'openclaw-extension' / 'src' / 'agent_file_store.py'
        self.assertEqual(hermes.read_bytes(), openclaw.read_bytes())


if __name__ == '__main__':
    unittest.main()
