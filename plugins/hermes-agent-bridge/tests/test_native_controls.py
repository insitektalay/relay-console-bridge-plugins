import asyncio, importlib.util, sys, tempfile, types, unittest, uuid
from pathlib import Path
SRC=Path(__file__).parents[1]/'src'
sys.path.insert(0,str(SRC))
import native_controls as controls
from native_operation_store import journal

class Controls(unittest.IsolatedAsyncioTestCase):
    async def test_claimed_profile_runs_once_and_replays_only_receipt(self):
        with tempfile.TemporaryDirectory() as state:
            request={'operationId':str(uuid.uuid4()),'accountId':'account','workspaceId':'workspace','agentId':'agent','runtimeType':'hermes','externalAgentId':'profile:test','action':'read','expiresAt':'2099-01-01T00:00:00Z','allowed':True,'canStart':True}
            request['requestHash']=controls.digest({'account':'account','workspace':'workspace','agent':'agent','action':'read','baseVersion':None,'changes':None})
            completed=[]; calls=[]
            async def post(path,body):
                if path.endswith('/claim'): return request
                completed.append(body);return {'recorded':True}
            async def apply(kind,command): calls.append(command);return {'status':'read','profile':{'name':'Test'}}
            for _ in range(2):
                result=await controls.handle('profile',request,'workspace',state,post,apply)
                self.assertEqual(result['status'],'recorded')
            self.assertEqual(len(calls),1);self.assertEqual(completed[0],completed[1])
    async def test_changed_scope_cannot_execute(self):
        with tempfile.TemporaryDirectory() as state:
            request={'operationId':str(uuid.uuid4()),'requestHash':'a'*64}
            async def post(*_): return {**request,'workspaceId':'another','runtimeType':'hermes'}
            async def apply(*_): self.fail('Must not execute wrong workspace')
            with self.assertRaises(ValueError): await controls.handle('profile',request,'workspace',state,post,apply)
    async def test_started_without_receipt_is_never_replayed(self):
        with tempfile.TemporaryDirectory() as state:
            request={'operationId':str(uuid.uuid4()),'requestHash':'b'*64}
            journal(state,'reserve','profile',request['operationId'],request['requestHash'])
            journal(state,'start','profile',request['operationId'],request['requestHash'])
            async def forbidden(*_): self.fail('Unknown work must not claim or execute again')
            with self.assertRaises(ValueError): await controls.handle('profile',request,'workspace',state,forbidden,forbidden)
    async def test_cron_validates_hash_and_completes_before_reply(self):
        with tempfile.TemporaryDirectory() as state:
            command={'operationId':str(uuid.uuid4()),'workspaceId':'workspace','agentId':'agent','runtimeType':'hermes','action':'list','expiresAt':'2099-01-01T00:00:00Z'}
            command['requestHash']=controls.digest({k:v for k,v in command.items() if k!='operationId'})
            events=[]
            async def post(path,body):
                events.append(path)
                return {**command,'allowed':True} if path.endswith('/claim') else {'recorded':True}
            async def apply(*_): return {'status':'confirmed','jobs':[]}
            result=await controls.handle('cron',command,'workspace',state,post,apply)
            self.assertEqual(result['jobs'],[]);self.assertTrue(events[-1].endswith('/complete'))

if __name__=='__main__': unittest.main()
