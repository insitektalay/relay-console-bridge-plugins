"""Bounded native text operations. Invoked after Railway grants an operation claim."""
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

MAX = 500_000
FAMILIES = {'instructions', 'memory', 'memories', 'skills'}
BLOCKED = {'sessions', 'logs', 'node_modules', 'backups', 'backup', 'cache', 'archives'}


def digest(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def parts_for(path, directory=False):
    parts = path.split('/') if path else []
    if len(path) > 240 or len(parts) > 8 or any(not re.fullmatch(r'[A-Za-z0-9_ .-]+', p) or p.startswith('.') or p.lower() in BLOCKED for p in parts):
        raise ValueError('path')
    parent = parts[0] if parts and (directory or len(parts) > 1) else ''
    if parent and parent.lower() not in FAMILIES:
        raise ValueError('family')
    if not directory and (not parts or not re.search(r'\.(md|markdown|txt)$', parts[-1], re.I) or len(parts) == 1 and not re.search(r'\.(md|markdown)$', parts[0], re.I)):
        raise ValueError('text')
    return parts


def directory_fd(root, parts):
    # Opening each parent relative to its retained descriptor prevents both
    # symlink traversal and parent-directory substitution during an operation.
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in [*Path(root).parts[1:], *parts]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_at(fd, name):
    try:
        file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(file)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX:
            raise ValueError('file')
        content = os.read(file, MAX + 1)
        if len(content) > MAX or b'\0' in content:
            raise ValueError('content')
        return content.decode('utf-8')
    finally:
        os.close(file)


def apply(root, command):
    action, path = command['action'], command['path']
    if action not in {'list', 'read', 'create', 'import', 'write', 'mkdir', 'delete'}:
        raise ValueError('action')
    parts = parts_for(path, action in {'list', 'mkdir'})
    if action == 'list':
        try:
            fd = directory_fd(root, parts)
        except FileNotFoundError:
            return {'status': 'listed', 'path': path, 'files': [], 'folders': []}
        try:
            files, folders = [], []
            for name in sorted(os.listdir(fd)):
                if len(files) + len(folders) >= 300:
                    break
                full = '/'.join([*parts, name])
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                try:
                    if stat.S_ISDIR(info.st_mode):
                        parts_for(full, True)
                        folders.append({'name': name, 'path': full})
                    elif stat.S_ISREG(info.st_mode):
                        parts_for(full)
                        content = read_at(fd, name)
                        files.append({'filename': name, 'path': full, 'size': len(content.encode('utf-8')), 'version': digest(content)})
                except (ValueError, OSError, UnicodeError):
                    continue
            return {'status': 'listed', 'path': path, 'files': files, 'folders': folders}
        finally:
            os.close(fd)
    if not parts:
        raise ValueError('path')
    fd = directory_fd(root, parts[:-1])
    try:
        name = parts[-1]
        if action == 'mkdir':
            if command.get('baseVersion') != 'absent':
                raise ValueError('base')
            os.mkdir(name, mode=0o700, dir_fd=fd)
            os.fsync(fd)
            return {'status': 'applied', 'path': path, 'version': 'absent'}
        current = read_at(fd, name)
        version = digest(current) if current is not None else 'absent'
        if action == 'read':
            if current is None:
                raise ValueError('missing')
            return {'status': 'read', 'path': path, 'version': version, 'content': current}
        base = command.get('baseVersion')
        if base != version or action in {'create', 'import'} and version != 'absent':
            return {'status': 'conflict', 'path': path, 'version': version}
        if action in {'write', 'delete'} and current is None:
            raise ValueError('missing')
        if action == 'delete':
            os.unlink(name, dir_fd=fd)
            os.fsync(fd)
            return {'status': 'applied', 'path': path, 'version': 'absent'}
        content = command.get('content')
        if not isinstance(content, str) or len(content.encode('utf-8')) > MAX or '\0' in content or digest(content) != command.get('contentHash'):
            raise ValueError('content')
        temp = '.relay-' + uuid.uuid4().hex
        file = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(file, 'wb') as stream:
                stream.write(content.encode('utf-8'))
                stream.flush()
                os.fsync(stream.fileno())
            latest = read_at(fd, name)
            if (digest(latest) if latest is not None else 'absent') != version:
                return {'status': 'conflict', 'path': path, 'version': digest(latest) if latest is not None else 'absent'}
            os.rename(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temp, dir_fd=fd)
            except FileNotFoundError:
                pass
        return {'status': 'applied', 'path': path, 'version': digest(content)}
    finally:
        os.close(fd)


def execute(root, state_dir, command):
    operation = str(uuid.UUID(command['operationId']))
    request_hash = command['requestHash']
    if not re.fullmatch(r'[a-f0-9]{64}', request_hash) or not os.path.isabs(root):
        raise ValueError('identity')
    Path(state_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = open(Path(state_dir) / 'agent-file.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX)
    db = sqlite3.connect(str(Path(state_dir) / 'agent-file-receipts.sqlite'), timeout=5)
    result = {'operationId': operation, 'requestHash': request_hash, 'path': command['path'], 'status': 'failed', 'code': 'AGENT_FILE_NOT_APPLIED', 'nativeInactive': True}
    try:
        db.execute('CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, hash TEXT NOT NULL, result TEXT, intent TEXT, settled INTEGER DEFAULT 0, started INTEGER DEFAULT 0)')
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT hash,result,started FROM receipts WHERE id=?', (operation,)).fetchone()
        if previous:
            if previous[0] != request_hash:
                raise ValueError('reused identity')
            if previous[1]:
                return json.loads(previous[1])
            if previous[2]:
                return {**result, 'status': 'outcome_unknown', 'code': 'AGENT_FILE_INTERRUPTED'}
        if command.get('_claimAllowed') is not True:
            return {**result, 'noStart': True}
        expires = datetime.fromisoformat(command['expiresAt'].replace('Z', '+00:00')).timestamp()
        if expires <= time.time():
            return {**result, 'noStart': True}
        db.execute('INSERT OR IGNORE INTO receipts(id,hash,intent) VALUES(?,?,?)', (operation, request_hash, json.dumps(result)))
        db.execute('UPDATE receipts SET started=1 WHERE id=?', (operation,))
        db.commit()  # Durable intent before any file effect.
        try:
            result.update(apply(root, command))
        except (OSError, ValueError, UnicodeError):
            # The error can follow a native effect, so do not claim no-start.
            result.update(status='outcome_unknown', code='AGENT_FILE_COMPLETION_NOT_PROVED')
        if result['status'] in {'read', 'listed', 'applied'}:
            result.pop('code', None)
        receipt = {k: v for k, v in result.items() if k not in {'content', 'files', 'folders'}}
        db.execute('UPDATE receipts SET result=? WHERE id=?', (json.dumps(receipt, sort_keys=True), operation))
        db.commit()
        return result
    finally:
        db.close()
        lock.close()


def recover(state_dir, settled_id=None, reserve=None):
    Path(state_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(Path(state_dir) / 'agent-file.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        db = sqlite3.connect(str(Path(state_dir) / 'agent-file-receipts.sqlite'))
        try:
            db.execute('CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, hash TEXT NOT NULL, result TEXT, intent TEXT, settled INTEGER DEFAULT 0, started INTEGER DEFAULT 0)')
            if reserve:
                operation = str(uuid.UUID(reserve['operationId']))
                request_hash = reserve['requestHash']
                if not re.fullmatch(r'[a-f0-9]{64}', request_hash):
                    raise ValueError('hash')
                receipt = {'operationId': operation, 'requestHash': request_hash, 'path': reserve['path'], 'status': 'failed', 'code': 'AGENT_FILE_NOT_APPLIED', 'nativeInactive': True}
                db.execute('INSERT OR IGNORE INTO receipts(id,hash,intent) VALUES(?,?,?)', (operation, request_hash, json.dumps(receipt)))
                db.commit()
                return []
            if settled_id:
                db.execute('UPDATE receipts SET settled=1 WHERE id=?', (settled_id,))
                db.commit()
                return []
            values = []
            for operation, saved, intent, started in db.execute('SELECT id,result,intent,started FROM receipts WHERE settled=0').fetchall():
                receipt = json.loads(saved) if saved else ({**json.loads(intent), 'status': 'outcome_unknown', 'code': 'AGENT_FILE_INTERRUPTED'} if started else {**json.loads(intent), 'noStart': True})
                if not saved:
                    db.execute('UPDATE receipts SET result=? WHERE id=?', (json.dumps(receipt, sort_keys=True), operation))
                values.append(receipt)
            db.commit()
            return values
        finally:
            db.close()


if __name__ == '__main__':
    request = json.loads(sys.stdin.buffer.read(750_001))
    if request.get('mode') == 'reserve':
        value = recover(request['stateDir'], reserve=request['command'])
    elif request.get('mode') == 'recover':
        value = recover(request['stateDir'])
    elif request.get('mode') == 'settle':
        value = recover(request['stateDir'], request['operationId'])
    else:
        value = execute(request['root'], request['stateDir'], request['command'])
    print(json.dumps(value))
