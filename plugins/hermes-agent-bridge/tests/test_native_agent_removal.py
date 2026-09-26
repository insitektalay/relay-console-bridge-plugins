import asyncio, tempfile, unittest, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from native_agent_removal import handle
from native_controls import digest
from native_operation_store import journal

class RemovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_claimed_removal_is_saved_and_not_repeated(self):
        with tempfile.TemporaryDirectory() as state:
            scope=dict(accountId='a',workspaceId='w',agentId='g',deviceId='d',bindingId='b',workspaceGeneration='1',assignmentEpoch='2',hostGeneration=1,runtimeType='hermes',externalAgentId='profile:test',action='remove',deleteFiles=True)
            op=str(uuid.uuid4()); command={**scope,'operationId':op,'requestHash':digest(scope),'allowed':True,'expiresAt':(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()}
            calls=[]; receipts=[]
            async def post(path,body):
                if path.endswith('/claim'): return command
                receipts.append(body); return dict(recorded=True,operationId=op)
            async def apply(c):
                calls.append(c); return dict(status='removed',externalAgentId='profile:test',filesRemoved=True)
            await handle(command,'w',state,post,apply)
            await handle(command,'w',state,post,apply)
            self.assertEqual(len(calls),1); self.assertEqual(receipts[0],receipts[1]);self.assertTrue(receipts[0]['filesRemoved'])
    async def test_changed_scope_cannot_delete(self):
        with tempfile.TemporaryDirectory() as state:
            op=str(uuid.uuid4()); envelope=dict(operationId=op,requestHash='a'*64)
            async def post(path,body):return dict(operationId=op,requestHash='a'*64,workspaceId='wrong',runtimeType='hermes')
            async def apply(c):self.fail('Unclaimed deletion')
            with self.assertRaises(ValueError): await handle(envelope,'w',state,post,apply)
    async def test_started_without_receipt_cannot_repeat(self):
        with tempfile.TemporaryDirectory() as state:
            op=str(uuid.uuid4());h='b'*64
            journal(state,'reserve','agent_removal',op,h);journal(state,'start','agent_removal',op,h)
            async def post(*a):self.fail('Unknown removal must not claim again')
            with self.assertRaises(ValueError): await handle(dict(operationId=op,requestHash=h),'w',state,post)

class NativeProfileDeletionTests(unittest.TestCase):
    def test_harness_api_deletes_only_selected_profile_and_checks_absence(self):
        import types
        from unittest.mock import patch, Mock
        from native_agent_removal import remove_profile
        import native_profiles
        with tempfile.TemporaryDirectory() as state:
            selected=Path(state)/'selected';selected.mkdir();other=Path(state)/'other';other.mkdir()
            api=types.ModuleType('hermes_cli.profiles');api.validate_profile_name=Mock();api.get_profile_dir=Mock(return_value=selected)
            def delete(name,yes):
                self.assertEqual(name,'test');self.assertTrue(yes);selected.rmdir();return selected
            api.delete_profile=Mock(side_effect=delete)
            with patch.dict(sys.modules,{'hermes_cli':types.ModuleType('hermes_cli'),'hermes_cli.profiles':api}),patch.object(native_profiles,'enumerate_native_profiles',return_value=[]):
                self.assertEqual(remove_profile(dict(externalAgentId='profile:test',deleteFiles=True))['status'],'removed')
                self.assertTrue(other.exists());api.delete_profile.assert_called_once()
                with self.assertRaises(ValueError):remove_profile(dict(externalAgentId='default',deleteFiles=True))
                api.delete_profile.assert_called_once()
    def test_native_files_remaining_never_count_as_success(self):
        import types
        from unittest.mock import patch, Mock
        from native_agent_removal import remove_profile
        with tempfile.TemporaryDirectory() as state:
            selected=Path(state)/'selected';selected.mkdir()
            api=types.ModuleType('hermes_cli.profiles');api.validate_profile_name=Mock();api.get_profile_dir=Mock(return_value=selected);api.delete_profile=Mock(return_value=selected)
            with patch.dict(sys.modules,{'hermes_cli':types.ModuleType('hermes_cli'),'hermes_cli.profiles':api}):
                with self.assertRaisesRegex(ValueError,'not confirmed'):remove_profile(dict(externalAgentId='profile:test',deleteFiles=True))
