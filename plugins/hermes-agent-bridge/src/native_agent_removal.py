"""Native profile deletion behind an authenticated, durable one-shot claim."""
import asyncio, contextlib, io, time
from datetime import datetime
from pathlib import Path
try:
    from .native_operation_store import journal
    from .native_controls import digest
except ImportError:
    from native_operation_store import journal
    from native_controls import digest


def remove_profile(command):
    from hermes_cli.profiles import delete_profile, get_profile_dir, validate_profile_name
    try:
        from .native_profiles import profile_name_from_external_id, enumerate_native_profiles
    except ImportError:
        from native_profiles import profile_name_from_external_id, enumerate_native_profiles
    name = profile_name_from_external_id(command['externalAgentId'])
    if not name or name.lower() == 'default' or command.get('deleteFiles') is not True:
        raise ValueError('Required profile cannot be removed')
    validate_profile_name(name)
    root = Path(get_profile_dir(name))
    if root.is_symlink(): raise ValueError('Profile symlink cannot be removed')
    if root.exists():
        # Use the harness API: it stops the profile gateway and removes its
        # service, wrapper, configuration, sessions, skills and cron files.
        with contextlib.redirect_stdout(io.StringIO()):
            removed = delete_profile(name, yes=True)
        if Path(removed).resolve() != root.resolve(): raise ValueError('Removal path changed')
    if root.exists() or any(p.external_id == command['externalAgentId'] for p in enumerate_native_profiles()):
        raise ValueError('Native profile removal was not confirmed')
    return {'status':'removed','externalAgentId':command['externalAgentId'],'filesRemoved':True}


async def handle(envelope, workspace, state, post, apply=None):
    operation, request_hash = envelope['operationId'], envelope['requestHash']
    saved = await asyncio.to_thread(journal,state,'reserve','agent_removal',operation,request_hash)
    route = f'bridge/agent-removal/{operation}'
    if saved.get('receipt'):
        await post(route+'/complete',saved['receipt'])
        return {'operationId':operation,'status':'recorded'}
    if saved['phase'] != 'prepared': raise ValueError('Unknown removal will not be repeated')
    command = await post(route+'/claim',{'requestHash':request_hash})
    signed = {k:v for k,v in command.items() if k not in {'operationId','requestHash','expiresAt','allowed'}}
    if command.get('operationId') != operation or command.get('workspaceId') != workspace or command.get('runtimeType') != 'hermes' or command.get('requestHash') != request_hash or digest(signed) != request_hash:
        raise ValueError('Removal claim changed')
    receipt = {'operationId':operation,'requestHash':request_hash,'nativeInactive':True}
    if command.get('allowed') is not True or datetime.fromisoformat(command['expiresAt'].replace('Z','+00:00')).timestamp() <= time.time():
        receipt.update(status='failed',noStart=True)
    else:
        await asyncio.to_thread(journal,state,'start','agent_removal',operation,request_hash)
        try:
            result = await apply(command) if apply else await asyncio.to_thread(remove_profile,command)
            receipt.update(result)
        except Exception:
            receipt.update(status='outcome_unknown')
    await asyncio.to_thread(journal,state,'finish','agent_removal',operation,request_hash,receipt)
    accepted = await post(route+'/complete',receipt)
    if accepted.get('recorded') is True and accepted.get('operationId') == operation:
        await asyncio.to_thread(journal,state,'acknowledge','agent_removal',operation,request_hash)
    return {'operationId':operation,'status':'recorded'}
