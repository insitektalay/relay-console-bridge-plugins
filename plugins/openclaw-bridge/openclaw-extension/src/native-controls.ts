import { removeNativeAgent } from './native-agent-removal.js';
import { createHash } from 'node:crypto';
import { spawn, execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import { configuredModelCatalog } from './model-catalog.js';

type Runtime = { current?:()=>any; loadConfig?:()=>any; mutateConfigFile?:(params:any)=>Promise<unknown>; writeConfigFile?:(config:any)=>Promise<void> };
let profileRuntime:Runtime|undefined;
export function configureNativeProfileRuntime(runtime:Runtime) { profileRuntime=runtime; }
const canonical=(v:any):any=>Array.isArray(v)?v.map(canonical):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])])):v;
export const nativeDigest=(v:any)=>createHash('sha256').update(JSON.stringify(canonical(v))).digest('hex');
function current() { const r=profileRuntime; if (!r) throw Error('Native config unavailable'); return structuredClone(r.current?r.current():r.loadConfig?.()); }
function entry(cfg:any,id:string) { const item=Array.isArray(cfg?.agents?.list)?cfg.agents.list.find((a:any)=>a.id===id):cfg?.agents?.entries?.[id]; if (!item) throw Error('Native agent unavailable'); return item; }
export function profileSnapshot(cfg:any,id:string) {
 const a=entry(cfg,id), model=a.model??cfg.agents?.defaults?.model;
 const primary=typeof model==='string'?model:model?.primary??null;
 const value={name:a.identity?.name??a.name??id,nativeName:a.identity?.name??a.name??id,role:a.identity?.theme??'assistant',avatarUrl:a.identity?.avatar??null,
  modelPrimary:primary,status:'active',models:[...new Set([...(configuredModelCatalog(cfg)?.models??[]),...(primary?[primary]:[])])],editableStatus:false};
 return {...value,version:nativeDigest({value,entry:a})};
}
export function changeProfile(cfg:any,command:any) {
 const before=profileSnapshot(cfg,command.externalAgentId);
 if(command.baseVersion!==before.version) throw Error('Profile changed; refresh before saving');
 const changes=command.changes;
 if(!changes||Array.isArray(changes)||!Object.keys(changes).length||Object.keys(changes).some(k=>!['name','role','avatarUrl','modelPrimary'].includes(k))) throw Error('Invalid profile fields');
 for(const [key,limit] of [['name',120],['role',160],['modelPrimary',160]] as const) if(key in changes&&(typeof changes[key]!=='string'||changes[key].length>limit||(key!=='role'&&!changes[key].trim()))) throw Error('Invalid profile text');
 if('modelPrimary' in changes&&!before.models.includes(changes.modelPrimary)) throw Error('Select a configured model');
 if(changes.avatarUrl!=null&&(typeof changes.avatarUrl!=='string'||changes.avatarUrl.length>4250000)) throw Error('Invalid avatar');
 const a=entry(cfg,command.externalAgentId);
 if('name' in changes) {a.name=changes.name; a.identity={...a.identity,name:changes.name};}
 if('role' in changes) a.identity={...a.identity,theme:changes.role};
 if('avatarUrl' in changes) {a.identity={...a.identity}; if(changes.avatarUrl===null) delete a.identity.avatar; else a.identity.avatar=changes.avatarUrl;}
 if('modelPrimary' in changes) a.model=typeof a.model==='object'?{...a.model,primary:changes.modelPrimary}:changes.modelPrimary;
 return profileSnapshot(cfg,command.externalAgentId);
}
async function applyProfile(command:any) {
 if(command.action==='read') return {status:'read',profile:profileSnapshot(current(),command.externalAgentId)};
 if(command.action!=='update') throw Error('Unsupported action');
 const r=profileRuntime!; let expected:any;
 if(r.mutateConfigFile) await r.mutateConfigFile({base:'runtime',mutate:(draft:any)=>{expected=changeProfile(draft,command);},afterWrite:{mode:'auto'}});
 else if(r.writeConfigFile){const cfg=current();expected=changeProfile(cfg,command);await r.writeConfigFile(cfg);}
 else throw Error('Native config writer unavailable');
 const actual=profileSnapshot(current(),command.externalAgentId);
 if(actual.version!==expected.version) throw Error('Native profile readback differs');
 return {status:'applied',profile:actual};
}
function store(input:any):Promise<any> { return new Promise((resolve,reject)=>{
 const child=spawn('python3',[fileURLToPath(new URL('./native_operation_store.py',import.meta.url))],{stdio:['pipe','pipe','pipe']});
 let output=''; const timer=setTimeout(()=>child.kill(),5000);
 child.stdout.on('data',b=>{output+=b;if(Buffer.byteLength(output)>5_000_000)child.kill();}); child.stderr.resume();
 child.on('error',e=>{clearTimeout(timer);reject(e);}); child.on('close',code=>{clearTimeout(timer);try{if(code!==0)throw Error('Native journal failed');resolve(JSON.parse(output));}catch(e){reject(e);}});
 child.stdin.on('error',()=>{});child.stdin.end(JSON.stringify(input));
 }); }
const queues=new Map<string,Promise<any>>();
type Input={kind:'profile'|'cron'|'agent_removal';command:any;workspaceId:string;stateDir:string;post:(path:string,body:any)=>Promise<any>;apply?:(command:any)=>Promise<any>};
export function handleNativeControl(input:Input):Promise<any>{
 const next=(queues.get(input.stateDir)??Promise.resolve()).catch(()=>{}).then(()=>perform(input)); queues.set(input.stateDir,next);
 void next.finally(()=>{if(queues.get(input.stateDir)===next)queues.delete(input.stateDir);}).catch(()=>{});return next;
}
async function cron(command:any) {
 if(command.action!=='list')throw Error('OpenClaw native cron is read-only');
 const {stdout}=await promisify(execFile)('openclaw',['cron','list','--all','--agent',command.externalAgentId,'--json','--timeout','15000'],{timeout:20000,maxBuffer:256000});
 const result=JSON.parse(stdout.slice(stdout.indexOf('{')));const jobs=result.jobs;
 if(!Array.isArray(jobs)||jobs.length>500)throw Error('Invalid cron inventory');
 return {status:'confirmed',runtimeType:'openclaw',jobs,canEdit:false,scheduler:{available:true,running:true,message:'Native OpenClaw gateway responded'}};
}
async function recover(stateDir:string,post:Input['post'],currentOperation:string) {
 // Runs inside the per-state operation queue; unknown started work stays fenced.
 for(const item of await store({stateDir,mode:'pending'})) {
  if(item.operationId===currentOperation)continue;
  const id={stateDir,kind:item.kind,operationId:item.operationId,requestHash:item.requestHash};
  const receipt=item.receipt??{operationId:item.operationId,requestHash:item.requestHash,status:item.kind==='cron'?'unconfirmed':'failed',noStart:true,nativeInactive:true};
  if(!item.receipt)await store({...id,mode:'finish',receipt});
  try {const accepted=await post(`bridge/${item.kind==='agent_removal'?'agent-removal':item.kind==='profile'?'agent-profile':'native-cron'}/${item.operationId}/complete`,receipt);if(accepted.recorded!==true||accepted.operationId!==item.operationId)continue;}catch{continue;}
  await store({...id,mode:'acknowledge'});
 }
}
async function perform(input:Input) {
 const {kind,command:envelope,workspaceId,stateDir,post}=input;
 await recover(stateDir,post,envelope.operationId);
 const id={stateDir,kind,operationId:envelope.operationId,requestHash:envelope.requestHash};
 const saved=await store({...id,mode:'reserve'});
 const complete=kind==='agent_removal'?`bridge/agent-removal/${id.operationId}/complete`:kind==='profile'?`bridge/agent-profile/${id.operationId}/complete`:`bridge/native-cron/${id.operationId}/complete`;
 if(saved.receipt){await post(complete,saved.receipt);if(kind!=='cron')return {operationId:id.operationId,status:'recorded'};throw Error('Read a fresh cron snapshot');}
 if(saved.phase!=='prepared')throw Error('Unknown native outcome will not be repeated');
 const claim=kind==='agent_removal'?await post(`bridge/agent-removal/${id.operationId}/claim`,{requestHash:id.requestHash}):kind==='profile'?await post(`bridge/agent-profile/${id.operationId}/claim`,{requestHash:id.requestHash}):await post('bridge/native-cron/claim',Object.fromEntries(Object.entries(envelope).filter(([k])=>['operationId','requestHash','agentId','bindingId','workspaceId','workspaceGeneration','assignmentEpoch','hostGeneration','externalAgentId','runtimeType','expiresAt'].includes(k))));
 if(claim.operationId!==id.operationId||claim.requestHash!==id.requestHash||claim.workspaceId!==workspaceId||claim.runtimeType!=='openclaw')throw Error('Native claim scope changed');
 const command=kind!=='cron'?claim:{...envelope,...claim};
 if(kind==='cron'&&envelope.bridgeDeviceId!==envelope.deviceId)throw Error('Cron delivery device changed');
 const signed=kind==='agent_removal'?Object.fromEntries(Object.entries(claim).filter(([k])=>!['operationId','requestHash','expiresAt','allowed'].includes(k))):kind==='profile'?{account:claim.accountId,workspace:workspaceId,agent:claim.agentId,action:claim.action,baseVersion:claim.baseVersion??null,changes:claim.changes??null}:Object.fromEntries(Object.entries(envelope).filter(([k])=>!['requestId','operationId','requestHash','bridgeDeviceId'].includes(k)));
 if(nativeDigest(signed)!==id.requestHash)throw Error('Native command digest changed');
 if(kind==='cron'&&Object.keys(claim).some(k=>k!=='allowed'&&claim[k]!==envelope[k]))throw Error('Cron claim changed');
 let receipt:any={operationId:id.operationId,requestHash:id.requestHash,nativeInactive:true}, result:any;
 const allowed=claim.allowed===true&&(kind!=='profile'||claim.canStart===true)&&Date.parse(claim.expiresAt)>Date.now();
 if(!allowed){receipt={...receipt,status:kind==='cron'?'unconfirmed':'failed',noStart:true};result=receipt;}
 else {
  await store({...id,mode:'start'});
  try{result=await(input.apply??(kind==='agent_removal'?removeNativeAgent:kind==='profile'?applyProfile:cron))(command);receipt.status=result.status;if(kind==='profile')receipt.profile=result.profile;if(kind==='agent_removal'){receipt.externalAgentId=result.externalAgentId;receipt.filesRemoved=result.filesRemoved;}}
  catch{receipt.status=kind==='cron'?'unconfirmed':'outcome_unknown';result=receipt;}
 }
 await store({...id,mode:'finish',receipt});await post(complete,receipt);await store({...id,mode:'acknowledge'});
 return kind!=='cron'?{operationId:id.operationId,status:'recorded'}:{...result,operationId:id.operationId,requestHash:id.requestHash,agentId:claim.agentId,runtimeType:'openclaw'};
}
