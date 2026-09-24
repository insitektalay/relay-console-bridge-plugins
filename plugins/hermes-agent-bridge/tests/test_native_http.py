import ast, asyncio, json, logging, time, types, unittest
from pathlib import Path


def methods():
    source = Path(__file__).parents[1] / 'src/main.py'
    cls = next(n for n in ast.parse(source.read_text()).body if isinstance(n, ast.ClassDef) and n.name == 'ClawChatHermesBridge')
    names = {'_post_native_control', '_send_native_reply', '_flush_native_replies'}
    found = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    assert len(found) == 3, 'Native HTTP refresh and reconnect reply handling are missing'
    scope = dict(asyncio=asyncio, Any=object, time=time, json=json, logger=logging.getLogger('test'))
    exec(compile(ast.Module(body=found, type_ignores=[]), str(source), 'exec'), scope)
    return {n:scope[n] for n in names}


class NativeHTTP(unittest.IsolatedAsyncioTestCase):
    async def test_expired_claim_refreshes_once_and_preserves_reply_until_reconnect(self):
        calls, sent = [], []
        class Response:
            def __init__(self, status): self.status = status
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            def raise_for_status(self):
                if self.status >= 400: raise RuntimeError('HTTP failure')
            async def json(self): return {'allowed':True}
        class Session:
            def post(self, url, *, json, headers):
                calls.append((json.copy(), headers['Authorization']))
                return Response(401 if len(calls)==1 else 200)
        class Bridge:
            config=types.SimpleNamespace(api_url='https://relay.example')
            session=Session()
            access_token='expired'
            async def _authenticate_device(self, session):
                self.refreshes += 1
                return {'tokens': {'accessToken':'fresh'}}
            async def _send_raw(self, message): sent.append(message)
        for name, fn in methods().items(): setattr(Bridge,name,fn)
        bridge=Bridge(); bridge.refreshes=0; bridge._native_http_lock=asyncio.Lock(); bridge._native_replies={}; bridge._native_reconnect_required=False
        result=await bridge._post_native_control('bridge/agent-files/id/claim', {'requestHash':'same'})
        self.assertTrue(result['allowed']); self.assertEqual(bridge.refreshes,1)
        self.assertEqual(calls,[({'requestHash':'same'},'Bearer expired'),({'requestHash':'same'},'Bearer fresh')])
        reply={'type':'clawchat.agent_file.result','data':{'requestId':'read','content':'bounded in memory'}}
        await bridge._send_native_reply(reply)
        self.assertEqual(sent,[])
        bridge._native_reconnect_required=False
        await bridge._flush_native_replies()
        self.assertEqual(sent,[reply])
        await bridge._flush_native_replies()
        self.assertEqual(sent,[reply])

if __name__=='__main__': unittest.main()
