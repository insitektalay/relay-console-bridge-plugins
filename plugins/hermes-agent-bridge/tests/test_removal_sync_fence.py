import ast,asyncio,unittest
from pathlib import Path

source=ast.parse((Path(__file__).resolve().parents[1]/'src/main.py').read_text())
cls=next(n for n in source.body if isinstance(n,ast.ClassDef) and n.name=='ClawChatHermesBridge')
method=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_exchange_agent_replicas')
namespace={};exec(compile(ast.Module(body=[method],type_ignores=[]),'<real-replica-method>','exec'),namespace)

class RemovalSyncFence(unittest.IsolatedAsyncioTestCase):
 async def test_sync_cannot_write_during_native_removal(self):
  class Fixture:
   def __init__(self):self._native_control_lock=asyncio.Lock();self.writes=0
   async def _exchange_agent_replicas_locked(self):self.writes+=1;return []
  f=Fixture()
  async with f._native_control_lock:
   task=asyncio.create_task(namespace['_exchange_agent_replicas'](f))
   await asyncio.sleep(0)
   self.assertEqual(f.writes,0)
  await task;self.assertEqual(f.writes,1)
