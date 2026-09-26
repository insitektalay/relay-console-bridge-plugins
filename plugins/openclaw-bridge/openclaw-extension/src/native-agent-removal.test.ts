import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {removeNativeAgent} from './native-agent-removal.js';
import {handleNativeControl,nativeDigest} from './native-controls.js';
test('required agents never reach native deletion',async()=>{
 for(const id of ['main','default','../other']) await assert.rejects(removeNativeAgent({action:'remove',deleteFiles:true,externalAgentId:id},async()=>{throw Error('must not call');}),/Required or invalid/);
});
test('shared workspace is refused before cron or agent changes',async()=>{
 const root=await mkdtemp(join(tmpdir(),'relay-removal-'));
 try {let calls=0;await assert.rejects(removeNativeAgent({action:'remove',deleteFiles:true,externalAgentId:'test'},async()=>{calls++;return [{id:'test',workspace:root,agentDir:root+'/test'},{id:'other',workspace:root,agentDir:root+'/other'}];}),/shared/);assert.equal(calls,1);}finally{await rm(root,{recursive:true});}
});
test('removal requires a verified absent native agent and files',async()=>{
 const root=await mkdtemp(join(tmpdir(),'relay-removal-'));const paths={workspace:root+'/workspace',agentDir:root+'/agent',sessionsDir:root+'/sessions'};
 let removed=false;const calls:string[][]=[];
 const run=async(args:string[])=>{calls.push(args);if(args[0]==='cron')return{jobs:[]};if(args[1]==='delete'){removed=true;return {agentId:'test',...paths};}return removed?[]:[{id:'test',...paths}];};
 try {assert.equal((await removeNativeAgent({action:'remove',deleteFiles:true,externalAgentId:'test'},run)).filesRemoved,true);assert.equal(calls.filter(c=>c[1]==='delete').length,1);}finally{await rm(root,{recursive:true});}
});
test('one claimed removal replays its receipt without another native effect',async()=>{
 const stateDir=await mkdtemp(join(tmpdir(),'relay-removal-'));
 const scope={accountId:'a',workspaceId:'w',agentId:'g',deviceId:'d',bindingId:'b',workspaceGeneration:'1',assignmentEpoch:'2',hostGeneration:1,runtimeType:'openclaw',externalAgentId:'test',action:'remove',deleteFiles:true};
 const command={...scope,operationId:randomUUID(),requestHash:nativeDigest(scope),allowed:true,expiresAt:new Date(Date.now()+60000).toISOString()};let applied=0;const receipts:any[]=[];
 const input={kind:'agent_removal' as const,command,stateDir,workspaceId:'w',post:async(path:string,body:any)=>{if(path.endsWith('/claim'))return command;receipts.push(body);return{recorded:true,operationId:command.operationId};},apply:async()=>{applied++;return{status:'removed',externalAgentId:'test',filesRemoved:true};}};
 try {await handleNativeControl(input);await handleNativeControl(input);assert.equal(applied,1);assert.deepEqual(receipts[0],receipts[1]);assert.equal(receipts[0].filesRemoved,true);}finally{await rm(stateDir,{recursive:true});}
});
