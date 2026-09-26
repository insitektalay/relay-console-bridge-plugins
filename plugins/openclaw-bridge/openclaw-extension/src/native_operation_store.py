"""Durable claim fence shared by remote profile and cron adapters.

A started operation without a receipt is never executed again or declared idle.
Commands and cron prompts are not stored here.
"""
import json, re, sqlite3, sys, uuid
from pathlib import Path


def journal(state_dir, mode, kind, operation, request_hash, receipt=None):
    operation = str(uuid.UUID(operation))
    if kind not in {'profile', 'cron', 'agent_removal'} or not re.fullmatch(r'[a-f0-9]{64}', request_hash):
        raise ValueError('Invalid operation identity')
    Path(state_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
    with sqlite3.connect(str(Path(state_dir) / 'native-controls.sqlite'), timeout=5) as db:
        db.execute('CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,kind TEXT,hash TEXT,phase TEXT,receipt TEXT)')
        if 'acknowledged' not in {r[1] for r in db.execute('PRAGMA table_info(operations)')}:
            db.execute('ALTER TABLE operations ADD COLUMN acknowledged INTEGER NOT NULL DEFAULT 0')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT kind,hash,phase,receipt FROM operations WHERE id=?', (operation,)).fetchone()
        if row and row[:2] != (kind, request_hash):
            raise ValueError('Changed operation identity')
        if not row:
            if mode != 'reserve': raise ValueError('Operation was not reserved')
            db.execute("INSERT INTO operations(id,kind,hash,phase,receipt) VALUES(?,?,?,'prepared',NULL)", (operation,kind,request_hash))
            return {'phase':'prepared'}
        if mode == 'acknowledge':
            if row[2] != 'completed': raise ValueError('Only a saved receipt can be acknowledged')
            db.execute('UPDATE operations SET acknowledged=1 WHERE id=?', (operation,))
            return {'acknowledged':True}
        if mode == 'start':
            if row[2] != 'prepared': raise ValueError('Operation cannot start twice')
            db.execute("UPDATE operations SET phase='started' WHERE id=?", (operation,))
            return {'phase':'started'}
        if mode == 'finish':
            if receipt.get('operationId') != operation or receipt.get('requestHash') != request_hash or receipt.get('nativeInactive') is not True:
                raise ValueError('Invalid completion receipt')
            encoded=json.dumps(receipt,sort_keys=True,separators=(',',':'))
            if row[3] and row[3] != encoded: raise ValueError('Receipt is immutable')
            db.execute("UPDATE operations SET phase='completed',receipt=? WHERE id=?", (encoded,operation))
            return {'phase':'completed','receipt':receipt}
        return {'phase':row[2], 'receipt':json.loads(row[3]) if row[3] else None}

def pending(state_dir):
    path=Path(state_dir)/'native-controls.sqlite'
    if not path.exists(): return []
    with sqlite3.connect(str(path),timeout=5) as db:
        if 'acknowledged' not in {r[1] for r in db.execute('PRAGMA table_info(operations)')}:
            db.execute('ALTER TABLE operations ADD COLUMN acknowledged INTEGER NOT NULL DEFAULT 0')
        return [{'operationId':r[0],'kind':r[1],'requestHash':r[2],'phase':r[3],
                 'receipt':json.loads(r[4]) if r[4] else None}
                for r in db.execute("SELECT id,kind,hash,phase,receipt FROM operations WHERE acknowledged=0 AND phase IN ('prepared','completed') ORDER BY rowid LIMIT 100")]

if __name__=='__main__':
    data=json.loads(sys.stdin.buffer.read(5_000_001))
    print(json.dumps(pending(data['stateDir']) if data['mode']=='pending' else journal(data['stateDir'],data['mode'],data['kind'],data['operationId'],data['requestHash'],data.get('receipt'))))
