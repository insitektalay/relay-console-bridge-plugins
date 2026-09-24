import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
import {handleNativeControl,nativeDigest,profileSnapshot,changeProfile,configureNativeProfileRuntime} from './native-controls.js';
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

test('profile write compares the same runtime-normalized base as the read',async()=>{
 const stateDir=await mkdtemp(join(tmpdir(),'profile-runtime-'));
 const source={agents:{entries:{one:{name:'One',identity:{theme:'Role'}}}}};
 let live:any={...structuredClone(source),agents:{...structuredClone(source.agents),defaults:{model:'provider/model'},entries:{one:{...source.agents.entries.one,workspace:'/native/one'}}}};
 let writes=0;
 configureNativeProfileRuntime({current:()=>live,mutateConfigFile:async(params:any)=>{
  const draft=structuredClone(params.base==='runtime'?live:source);params.mutate(draft);
  assert.equal(params.afterWrite.mode,'auto');live=draft;writes++;
 }});
 const claim={operationId:randomUUID(),accountId:'account',workspaceId:'workspace',agentId:'agent',runtimeType:'openclaw',externalAgentId:'one',action:'update',baseVersion:profileSnapshot(live,'one').version,changes:{role:'Updated'},expiresAt:'2099-01-01T00:00:00Z',allowed:true,canStart:true,requestHash:''};
 claim.requestHash=nativeDigest({account:'account',workspace:'workspace',agent:'agent',action:claim.action,baseVersion:claim.baseVersion,changes:claim.changes});
 let receipt:any;
 try{
  await handleNativeControl({kind:'profile',command:claim,stateDir,workspaceId:'workspace',post:async(path,body)=>{if(path.endsWith('/claim'))return claim;receipt=body;return{recorded:true};}});
  assert.equal(receipt.status,'applied');assert.equal(writes,1);assert.equal(live.agents.entries.one.identity.theme,'Updated');
 }finally{await rm(stateDir,{recursive:true,force:true});}
});

test('receipt recovery closes prepared work and preserves unknown started work',async()=>{
 const stateDir=await mkdtemp(join(tmpdir(),'control-recovery-'));const old= randomUUID(),started=randomUUID();
 const journal=(mode:string,id:string)=>JSON.parse(execFileSync('python3',[fileURLToPath(new URL('./native_operation_store.py',import.meta.url))],{input:JSON.stringify({stateDir,mode,kind:'cron',operationId:id,requestHash:'a'.repeat(64)}),encoding:'utf8'}));
 journal('reserve',old);journal('reserve',started);journal('start',started);
 const claim={operationId:randomUUID(),accountId:'a',workspaceId:'w',agentId:'g',runtimeType:'openclaw',action:'read',expiresAt:'2099-01-01T00:00:00Z',allowed:true,canStart:true,requestHash:nativeDigest({account:'a',workspace:'w',agent:'g',action:'read',baseVersion:null,changes:null})};
 const recovered:any[]=[];
 try{
  await handleNativeControl({kind:'profile',command:claim,stateDir,workspaceId:'w',post:async(path,body)=>{if(path.endsWith('/claim'))return claim;if(path.includes(old))recovered.push(body);return{recorded:true,operationId:body.operationId};},apply:async()=>({status:'read',profile:{name:'One'}})});
  assert.equal(recovered.length,1);assert.equal(recovered[0].noStart,true);assert.equal(recovered[0].nativeInactive,true);assert.equal(journal('reserve',started).phase,'started');
 }finally{await rm(stateDir,{recursive:true,force:true});}
});
