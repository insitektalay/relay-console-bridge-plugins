import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { access, realpath } from 'node:fs/promises';
import { resolve, sep } from 'node:path';
const exec = promisify(execFile);
async function cli(args:string[]):Promise<any> {
 const {stdout}=await exec('openclaw',args,{timeout:25000,maxBuffer:512000});
 // CLI diagnostics can precede the structured output.
 for (const match of stdout.matchAll(/(^|\n)([\[{])/g)) {
  try { return JSON.parse(stdout.slice(match.index! + match[1].length)); } catch {}
 }
 throw Error('Native command returned no structured result');
}
const exists=async(path:string)=>{try{await access(path);return true;}catch(e:any){if(e.code==='ENOENT')return false;throw e;}};
const canonical=async(path:string)=>{try{return await realpath(path);}catch(e:any){if(e.code==='ENOENT')return resolve(path);throw e;}};
const overlaps=(a:string,b:string)=>a===b||a.startsWith(b+sep)||b.startsWith(a+sep);
export async function removeNativeAgent(command:any, run=cli) {
 const id=command.externalAgentId;
 if(command.action!=='remove'||command.deleteFiles!==true||typeof id!=='string'||!/^[a-z0-9][a-z0-9_-]{0,127}$/.test(id)||['main','default'].includes(id))throw Error('Required or invalid native agent');
 const inventory=await run(['agents','list','--json']);
 if(!Array.isArray(inventory))throw Error('Native inventory unavailable');
 const selected=inventory.find(a=>a.id===id);
 if(!selected)throw Error('Native identity is absent without a saved removal receipt');
 if(typeof selected.workspace!=='string'||typeof selected.agentDir!=='string')throw Error('Native paths unavailable');
 const workspace=await canonical(selected.workspace), agentDir=await canonical(selected.agentDir);
 for(const other of inventory.filter(a=>a.id!==id)) {
  for(const path of [other.workspace,other.agentDir]) {
   if(typeof path==='string'&&(overlaps(workspace,await canonical(path))||overlaps(agentDir,await canonical(path))))throw Error('Native files are shared with another agent');
  }
 }
 const scheduled=await run(['cron','list','--all','--agent',id,'--json','--timeout','15000']);
 if(!Array.isArray(scheduled.jobs)||scheduled.jobs.some((j:any)=>j.agentId!==id||typeof j.id!=='string'))throw Error('Cron ownership changed');
 if(scheduled.jobs.some((j:any)=>j.state?.runningAtMs))throw Error('Native cron job is running');
 for(const job of scheduled.jobs) await run(['cron','rm',job.id,'--json','--timeout','15000']);
 const removed=await run(['agents','delete',id,'--force','--json']);
 if(removed.agentId!==id||removed.workspaceRetained===true)throw Error('Native removal did not confirm file deletion');
 const after=await run(['agents','list','--json']);
 if(!Array.isArray(after)||after.some(a=>a.id===id))throw Error('Native agent remains registered');
 for(const path of [selected.workspace,selected.agentDir,removed.sessionsDir]) {
  if(typeof path!=='string'||await exists(path))throw Error('Native agent files remain');
 }
 return {status:'removed',externalAgentId:id,filesRemoved:true};
}
