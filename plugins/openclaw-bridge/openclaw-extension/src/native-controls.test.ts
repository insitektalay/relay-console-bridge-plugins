import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {handleNativeControl,nativeDigest,profileSnapshot,changeProfile} from './native-controls.js';
test('profile mutation preserves credentials and other agents; rejects stale or invalid edit',()=>{
 const cfg={channels:{clawchat:{deviceToken:'fixture-only'}},agents:{defaults:{model:'openrouter/example'},entries:{one:{name:'One',identity:{theme:'Role'}},two:{name:'Two'}}}};
 const before=profileSnapshot(cfg,'one');
 const result=changeProfile(cfg,{externalAgentId:'one',baseVersion:before.version,changes:{name:'Changed',role:'New role'}});
 assert.equal(result.name,'Changed');assert.equal(result.role,'New role');assert.equal(cfg.channels.clawchat.deviceToken,'fixture-only');assert.equal(cfg.agents.entries.two.name,'Two');
 const after=structuredClone(cfg);
 assert.throws(()=>changeProfile(cfg,{externalAgentId:'one',baseVersion:before.version,changes:{name:'stale'}}));assert.deepEqual(cfg,after);
 assert.throws(()=>changeProfile(cfg,{externalAgentId:'one',baseVersion:result.version,changes:{name:'Do not save',modelPrimary:'not-configured'}}));assert.deepEqual(cfg,after);
});
test('profile operation executes once and completes before acknowledgement',async()=>{
 const stateDir=await mkdtemp(join(tmpdir(),'native-controls-'));let calls=0;let completions=0;
 const claim={operationId:randomUUID(),accountId:'account',workspaceId:'workspace',agentId:'agent',runtimeType:'openclaw',externalAgentId:'one',action:'read',expiresAt:'2099-01-01T00:00:00Z',allowed:true,canStart:true,requestHash:''};
 claim.requestHash=nativeDigest({account:'account',workspace:'workspace',agent:'agent',action:'read',baseVersion:null,changes:null});
 const input={kind:'profile' as const,command:claim,stateDir,workspaceId:'workspace',post:async(path:string)=>{if(path.endsWith('/claim'))return claim;completions++;return{recorded:true};},apply:async()=>{calls++;return {status:'read',profile:{name:'One'}};}};
 try{
  assert.equal((await handleNativeControl(input)).status,'recorded');assert.equal(completions,1);
  await handleNativeControl(input);assert.equal(calls,1);assert.equal(completions,2);
  await assert.rejects(handleNativeControl({...input,command:{...claim,operationId:randomUUID()},workspaceId:'other'}));assert.equal(calls,1);
 }finally{await rm(stateDir,{recursive:true,force:true});}
});

test('cron verifies signed payload separately from checked delivery metadata',async()=>{
 const stateDir=await mkdtemp(join(tmpdir(),'cron-controls-'));
 const signed={workspaceId:'workspace',agentId:'agent',deviceId:'device',runtimeType:'openclaw',action:'list',expiresAt:'2099-01-01T00:00:00Z'};
 const command={...signed,operationId:randomUUID(),requestHash:nativeDigest(signed),requestId:'delivery',bridgeDeviceId:'device'};
 let applied=0;let completed=0;
 const input={kind:'cron' as const,command,stateDir,workspaceId:'workspace',post:async(path:string,body:any)=>{if(path.endsWith('/claim'))return {...signed,operationId:body.operationId,requestHash:command.requestHash,allowed:true};completed++;return{recorded:true};},apply:async()=>{applied++;return {status:'confirmed',jobs:[]};}};
 try{
  assert.deepEqual((await handleNativeControl(input)).jobs,[]);assert.equal(applied,1);assert.equal(completed,1);
  await assert.rejects(handleNativeControl({...input,command:{...command,operationId:randomUUID(),bridgeDeviceId:'wrong'}}),/delivery device changed/);assert.equal(applied,1);
 }finally{await rm(stateDir,{recursive:true,force:true});}
});
