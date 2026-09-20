import test from 'node:test';
import assert from 'node:assert/strict';
import { FileResponseRelay } from './file-response-relay.js';
test('file result survives credential refresh without repeating the operation', () => {
 let now = 0; const relay = new FileResponseRelay(() => now);
 const old: any[] = [], next: any[] = [], foreign: any[] = [];
 const closeOld = relay.connect('device-a/workspace-a', x => old.push(x));
 const reply = { type: 'clawchat.agent_file.result', data: { requestId: 'request', content: 'saved text' } };
 relay.deliver('device-a/workspace-a', reply);
 relay.connect('device-b/workspace-a', x => foreign.push(x));
 relay.connect('device-a/workspace-a', x => next.push(x));
 closeOld();
 assert.deepEqual(next, [reply]); assert.equal(foreign.length, 0);
 relay.deliver('device-a/workspace-a', { ...reply, data: { requestId: 'next' } });
 assert.equal(next.length, 2); assert.equal(old.length, 1);
 now = 31_000; const late: any[] = [];
 relay.connect('device-a/workspace-a', x => late.push(x));
 assert.equal(late.length, 0);
});
