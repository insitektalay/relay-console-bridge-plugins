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
            command={'operationId':str(uuid.uuid4()),'workspaceId':'workspace','agentId':'agent','runtimeType':'hermes','deviceId':'device','action':'list','expiresAt':'2099-01-01T00:00:00Z'}
            command['requestHash']=controls.digest({k:v for k,v in command.items() if k!='operationId'})
            envelope={**command,'requestId':'delivery','bridgeDeviceId':'device'}
            events=[]
            async def post(path,body):
                events.append(path)
                return {**command,'allowed':True} if path.endswith('/claim') else {'recorded':True}
            async def apply(*_): return {'status':'confirmed','jobs':[]}
            result=await controls.handle('cron',envelope,'workspace',state,post,apply)
            self.assertEqual(result['jobs'],[]);self.assertTrue(events[-1].endswith('/complete'))
            with self.assertRaisesRegex(ValueError,'delivery device changed'):
                await controls.handle('cron',{**envelope,'operationId':str(uuid.uuid4()),'bridgeDeviceId':'wrong'},'workspace',state,lambda *args: asyncio.sleep(0,result={**command,'operationId':args[1]['operationId'],'allowed':True}),apply)

    async def test_recovery_preserves_receipts_and_never_invents_started_completion(self):
        with tempfile.TemporaryDirectory() as state:
            prepared,completed,started=[str(uuid.uuid4()) for _ in range(3)]
            for operation,kind in [(prepared,'cron'),(completed,'profile'),(started,'profile')]:
                journal(state,'reserve',kind,operation,'a'*64)
            receipt={'operationId':completed,'requestHash':'a'*64,'status':'outcome_unknown','nativeInactive':True}
            journal(state,'start','profile',completed,'a'*64)
            journal(state,'finish','profile',completed,'a'*64,receipt)
            journal(state,'start','profile',started,'a'*64)
            calls=[]
            async def post(path,body): calls.append((path,body));return {'recorded':True,'operationId':body['operationId']}
            await controls.recover(state,post,'current')
            self.assertEqual(len(calls),2)
            self.assertTrue(calls[0][1]['noStart']);self.assertEqual(calls[1][1],receipt)
            await controls.recover(state,post,'current')
            self.assertEqual(len(calls),2)
            self.assertEqual(journal(state,'reserve','profile',started,'a'*64)['phase'],'started')

if __name__=='__main__': unittest.main()
