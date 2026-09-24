"""Atomic edits to one existing Hermes profile's native configuration."""
import fcntl, hashlib, json, os, tempfile
from pathlib import Path
import yaml


def profile_values(profile, config):
    saved=config.get('relay_profile') or {}
    model=config.get('model') or profile.model
    model=model.get('default') if isinstance(model,dict) else model
    value={'name':saved.get('name') or profile.display_name,'nativeName':saved.get('name') or profile.display_name,
           'role':saved.get('role',profile.description),'avatarUrl':saved.get('avatarUrl'),
           'modelPrimary':model,'status':'active','models':[model] if model else [],'editableStatus':False}
    value['version']=hashlib.sha256(json.dumps({'profile':value,'config':config},sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    return value


def apply(profile, command):
    root=Path(profile.home); path=root/'config.yaml'
    if path.is_symlink(): raise ValueError('Profile config symlink is not supported')
    with open(root/'.relay-profile.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        config=yaml.safe_load(path.read_text()) or {}
        value=profile_values(profile,config)
        if command['action']=='read': return {'status':'read','profile':value}
        if command['action']!='update' or command.get('baseVersion')!=value['version']:
            raise ValueError('Profile changed; refresh before saving')
        fields=command.get('changes')
        if not isinstance(fields,dict) or not fields or set(fields)-{'name','role','avatarUrl','modelPrimary'}: raise ValueError('Invalid profile fields')
        for key, limit in [('name',120),('role',160),('modelPrimary',160)]:
            if key in fields and (not isinstance(fields[key],str) or len(fields[key])>limit or key!='role' and not fields[key].strip()): raise ValueError('Invalid profile text')
        if 'modelPrimary' in fields and fields['modelPrimary'] not in value['models']: raise ValueError('Select a configured runtime model')
        if fields.get('avatarUrl') is not None and (not isinstance(fields['avatarUrl'],str) or len(fields['avatarUrl'])>4_250_000): raise ValueError('Invalid avatar')
        config['relay_profile']={**(config.get('relay_profile') or {}),**{k:v for k,v in fields.items() if k!='modelPrimary'}}
        if 'modelPrimary' in fields:
            config['model']={**config['model'],'default':fields['modelPrimary']} if isinstance(config.get('model'),dict) else fields['modelPrimary']
        fd, temporary=tempfile.mkstemp(prefix='.relay-profile-',dir=root)
        try:
            with os.fdopen(fd,'w') as output:
                yaml.safe_dump(config,output,allow_unicode=True,sort_keys=False); output.flush(); os.fsync(output.fileno())
            os.replace(temporary,path)
            parent=os.open(root,os.O_DIRECTORY); os.fsync(parent); os.close(parent)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        return {'status':'applied','profile':profile_values(profile,config)}
