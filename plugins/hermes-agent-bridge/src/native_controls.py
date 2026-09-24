"""Claimed Hostinger controls. Native changes run only after scope validation."""
import asyncio, hashlib, json, time
from datetime import datetime
try:
    from .native_operation_store import journal
except ImportError:
    from native_operation_store import journal


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


async def handle(kind, envelope, workspace, state, post, apply):
    operation, request_hash = envelope['operationId'], envelope['requestHash']
    saved = await asyncio.to_thread(journal,state,'reserve',kind,operation,request_hash)
    completion = f'bridge/agent-profile/{operation}/complete' if kind=='profile' else f'bridge/native-cron/{operation}/complete'
    if saved.get('receipt'):
        await post(completion,saved['receipt'])
        if kind=='profile': return {'operationId':operation,'status':'recorded'}
        # A stored cron receipt deliberately excludes inventory and prompts.
        raise ValueError('Cron operation already completed; request a fresh snapshot')
    if saved['phase'] != 'prepared':
        raise ValueError('Native operation outcome is unknown; it will not be repeated')
    command = await post(f'bridge/agent-profile/{operation}/claim', {'requestHash':request_hash}) if kind=='profile' else await post('bridge/native-cron/claim', {k:v for k,v in envelope.items() if k in {'operationId','requestHash','agentId','bindingId','workspaceId','workspaceGeneration','assignmentEpoch','hostGeneration','externalAgentId','runtimeType','expiresAt'}})
    if command.get('operationId') != operation or command.get('requestHash') != request_hash or command.get('workspaceId') != workspace or command.get('runtimeType') != 'hermes':
        raise ValueError('Native claim scope changed')
    if kind=='profile':
        signed={'account':command['accountId'],'workspace':workspace,'agent':command['agentId'],'action':command['action'],'baseVersion':command.get('baseVersion'),'changes':command.get('changes')}
        allowed=command.get('allowed') is True and command.get('canStart') is True
    else:
        if envelope.get('bridgeDeviceId') != envelope.get('deviceId'): raise ValueError('Cron delivery device changed')
        signed={k:v for k,v in envelope.items() if k not in {'requestId','operationId','requestHash','bridgeDeviceId'}}
        if any(command.get(k) != envelope.get(k) for k in command if k!='allowed'): raise ValueError('Cron claim changed')
        command={**envelope,**command}; allowed=command.get('allowed') is True
    if digest(signed) != request_hash: raise ValueError('Native command digest changed')
    expires=datetime.fromisoformat(command['expiresAt'].replace('Z','+00:00')).timestamp()
    receipt={'operationId':operation,'requestHash':request_hash,'nativeInactive':True}
    if not allowed or expires <= time.time():
        receipt.update(status='failed' if kind=='profile' else 'unconfirmed',noStart=True)
        result=receipt
    else:
        await asyncio.to_thread(journal,state,'start',kind,operation,request_hash)
        try:
            result=await apply(kind,command)
            receipt.update(status=result['status'])
            if kind=='profile': receipt['profile']=result['profile']
        except Exception:
            # Adapter returns only after its thread/process is inactive.
            receipt.update(status='outcome_unknown' if kind=='profile' else 'unconfirmed')
            result=receipt
    await asyncio.to_thread(journal,state,'finish',kind,operation,request_hash,receipt)
    await post(completion,receipt)
    if kind=='profile': return {'operationId':operation,'status':'recorded'}
    return {**result,'operationId':operation,'requestHash':request_hash,'agentId':command['agentId'],'runtimeType':'hermes'}
